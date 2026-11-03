# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""GET-only bounded provider transport; OAuth uses its separate native client."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

import httpx

from pyfly.agentic.content.sources.dtos import Account, bounded
from pyfly.agentic.content.sources.errors import ErrorCode, SourceError
from pyfly.agentic.content.sources.ports import AsyncByteSink, DelegatedTokenSource
from pyfly.oauth2.acquisition import _close_safely

_SOURCE_HTTP: ContextVar[bool] = ContextVar("pyfly_source_http", default=False)


class _SourceLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _SOURCE_HTTP.get()


@contextmanager
def _private_http() -> Iterator[None]:
    # HTTPX logs complete request URLs at INFO; HTTPCore can log signed
    # redirect headers at DEBUG. Suppress only this task's provider requests.
    names = {"httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith("httpcore."))
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _SourceLogFilter) for f in logger.filters):
            logger.addFilter(_SourceLogFilter())
    token = _SOURCE_HTTP.set(True)
    try:
        yield
    finally:
        _SOURCE_HTTP.reset(token)


def checked_url(url: str, *, host: str, path: str | None = None) -> None:
    try:
        bounded(url, limit=16384)
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != host
            or parts.port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or "\\" in url
            or any(c.isspace() for c in url)
            or (path is not None and parts.path != path)
        ):
            raise ValueError
    except ValueError:
        raise SourceError("unsafe_url") from None


def redirect_host(url: str) -> str:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        raise SourceError("unsafe_url") from None
    if host is None:
        raise SourceError("unsafe_url")
    return host


def checked_hosts(hosts: frozenset[str]) -> frozenset[str]:
    for host in hosts:
        checked_url("https://" + host, host=host)
        if "." not in host or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError("Download hosts must be explicit public DNS names")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue
        raise ValueError("Download hosts cannot be IP literals")
    return hosts


class SourceTransport:
    def __init__(
        self,
        account: Account,
        tokens: DelegatedTokenSource,
        *,
        host: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
        max_json_bytes: int = 1024 * 1024,
        download_hosts: frozenset[str] = frozenset(),
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0 or isinstance(max_json_bytes, bool) or max_json_bytes <= 0:
            raise ValueError("Positive finite transport limits are required")
        self.account = account
        self.tokens = tokens
        self.host = host
        self.timeout = timeout
        self.max_json_bytes = max_json_bytes
        self.download_hosts = checked_hosts(download_hosts)
        self.client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=timeout)

    async def aclose(self) -> None:
        await _close_safely(self.client.aclose)

    @asynccontextmanager
    async def request(self, url: str, *, authenticated: bool = True) -> AsyncIterator[httpx.Response]:
        host = self.host if authenticated else redirect_host(url)
        if host is None or (not authenticated and host not in self.download_hosts):
            raise SourceError("unsafe_url")
        checked_url(url, host=host)
        headers = {"Accept-Encoding": "identity"}
        if authenticated:
            token = await self.tokens.access_token(self.account)
            try:
                bounded(token, limit=65536)
            except ValueError:
                raise SourceError("reconnect_required") from None
            headers["Authorization"] = "Bearer " + token
        try:
            with _private_http():
                async with asyncio.timeout(self.timeout):
                    response = await self.client.send(
                        self.client.build_request("GET", url, headers=headers), stream=True
                    )
                    try:
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise SourceError("invalid_response")
                        self.check_status(response)
                        yield response
                    finally:
                        await _close_safely(response.aclose)
        except TimeoutError:
            raise SourceError("timeout") from None
        except httpx.HTTPError:
            raise SourceError("transport_error") from None

    @staticmethod
    def check_status(response: httpx.Response) -> None:
        status = response.status_code
        if status in {200, 302, 307}:
            return
        codes: dict[int, ErrorCode] = {
            401: "reconnect_required",
            403: "access_denied",
            404: "unavailable",
            410: "resync_required",
            412: "version_changed",
            429: "throttled",
        }
        retry = response.headers.get("retry-after", "")
        seconds = min(int(retry), 86400) if len(retry) <= 6 and retry.isdecimal() else None
        raise SourceError(codes.get(status, "invalid_response"), retry_after=seconds if status == 429 else None)

    @staticmethod
    async def chunks(response: httpx.Response) -> AsyncIterator[bytes]:
        # Mock/custom transports can return prebuffered bodies; their allocation
        # is trusted transport work, not the network client's buffering policy.
        if response.is_stream_consumed:
            yield response.content
        else:
            if not isinstance(response.stream, httpx.AsyncByteStream):
                raise SourceError("invalid_response")
            # Own close in request(): aiter_raw closes outside our cancellation shield.
            async for chunk in response.stream:
                yield chunk

    async def json(self, url: str) -> dict[str, Any]:
        async with self.request(url) as response:
            if response.status_code != 200:
                raise SourceError("unsafe_url")
            data = bytearray()
            async for chunk in self.chunks(response):
                if len(chunk) > self.max_json_bytes - len(data):
                    raise SourceError("size_exceeded")
                data.extend(chunk)
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError):
                raise SourceError("invalid_response") from None
            if not isinstance(value, dict):
                raise SourceError("invalid_response")
            return value

    async def transfer(
        self,
        url: str,
        sink: AsyncByteSink,
        *,
        max_bytes: int,
        expected_size: int | None = None,
        redirects: bool = False,
    ) -> tuple[int, str]:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if expected_size is not None and expected_size > max_bytes:
            raise SourceError("size_exceeded")
        async with self.request(url) as response:
            if response.status_code in {302, 307}:
                if not redirects:
                    raise SourceError("unsafe_url")
                target = response.headers.get("location", "")
            else:
                return await self._copy(response, sink, max_bytes, expected_size)
        # Never reuse the authenticated request headers or follow another redirect.
        async with self.request(target, authenticated=False) as response:
            if response.status_code != 200:
                raise SourceError("unsafe_url")
            return await self._copy(response, sink, max_bytes, expected_size)

    async def _copy(
        self, response: httpx.Response, sink: AsyncByteSink, max_bytes: int, expected_size: int | None
    ) -> tuple[int, str]:
        declared = response.headers.get("content-length")
        if declared is not None and (not declared.isdecimal() or len(declared) > 20):
            raise SourceError("invalid_response")
        if declared is not None and int(declared) > max_bytes:
            raise SourceError("size_exceeded")
        size = 0
        digest = hashlib.sha256()
        async for chunk in self.chunks(response):
            if len(chunk) > max_bytes - size:
                raise SourceError("size_exceeded")
            size += len(chunk)
            digest.update(chunk)
            for offset in range(0, len(chunk), 65536):
                await sink.write(chunk[offset : offset + 65536])
        if (declared is not None and size != int(declared)) or (expected_size is not None and size != expected_size):
            raise SourceError("invalid_response")
        return size, digest.hexdigest()
