# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Bounded authorization-code/PKCE and RFC 8628 device acquisition."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import httpx

_ERRORS = frozenset(
    {
        "authorization_pending",
        "slow_down",
        "access_denied",
        "expired_token",
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
        "invalid_response",
        "transport_error",
        "timeout",
    }
)


class OAuth2ClientError(Exception):
    """Redacted error: provider bodies and exception strings are never included."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERRORS else "invalid_response"
        super().__init__(self.code)


@dataclass(frozen=True)
class PKCEPair:
    verifier: str = field(repr=False)
    challenge: str


def pkce_challenge(verifier: str) -> str:
    """Return an S256 challenge for a valid RFC 7636 verifier."""
    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        raise ValueError("Invalid PKCE verifier")
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def generate_pkce() -> PKCEPair:
    verifier = secrets.token_urlsafe(32)
    return PKCEPair(verifier, pkce_challenge(verifier))


@dataclass(frozen=True)
class OAuth2Endpoints:
    """Explicitly trusted endpoints; no discovery or token-supplied URLs are followed."""

    token_endpoint: str
    device_authorization_endpoint: str | None = None


@dataclass(frozen=True)
class OAuth2Tokens:
    access_token: str = field(repr=False)
    token_type: str
    expires_in: float | None = None
    refresh_token: str | None = field(default=None, repr=False)
    id_token: str | None = field(default=None, repr=False)
    scope: str | None = None


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_at: float
    interval: float
    verification_uri_complete: str | None = field(default=None, repr=False)
    _owner: object = field(default=None, repr=False, compare=False)


def _endpoint(url: str, allow_loopback_http: bool) -> None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        local = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in url)
            or (parsed.scheme != "https" and not (allow_loopback_http and local and parsed.scheme == "http"))
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "OAuth endpoints require HTTPS (or explicit loopback HTTP) without userinfo or fragments"
        ) from None


def _text(doc: dict[str, Any], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value or len(value) > 65536:
        raise OAuth2ClientError("invalid_response")
    return value


def _positive(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise OAuth2ClientError("invalid_response")
    try:
        number = float(value)
    except OverflowError:
        raise OAuth2ClientError("invalid_response") from None
    if not math.isfinite(number) or number <= 0:
        raise OAuth2ClientError("invalid_response")
    return number


async def _close_safely(close: Callable[[], Awaitable[None]]) -> None:
    from anyio import CancelScope

    # A timeout or direct Task.cancel() may arrive during resource release.
    # Wait for close to finish, then preserve the caller's cancellation.
    with CancelScope(shield=True):
        closing = asyncio.ensure_future(close())
        cancelled: asyncio.CancelledError | None = None
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as exc:
                cancelled = exc
        closing.result()
        if cancelled is not None:
            raise cancelled


class OAuth2Client:
    """Async acquisition client owning its HTTPX client and supplied transport.

    No automatic redirects, auth challenge flows, retries, environment proxies or
    decompression. Responses are capped before accumulation. Transport chunks are
    allocated by the transport, which must itself be trusted. ``aclose`` or async
    context exit releases resources. Cancellation propagates without retry.
    Timeouts bound acquisition, but cancellation waits for resource cleanup;
    trusted transports must provide terminating close operations.
    """

    def __init__(
        self,
        client_id: str,
        endpoints: OAuth2Endpoints,
        *,
        client_secret: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 65536,
        allow_loopback_http: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        import httpx

        if (
            not client_id
            or not math.isfinite(timeout)
            or timeout <= 0
            or isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError("client_id and positive finite response limits are required")
        _endpoint(endpoints.token_endpoint, allow_loopback_http)
        if endpoints.device_authorization_endpoint is not None:
            _endpoint(endpoints.device_authorization_endpoint, allow_loopback_http)
        self._client_id = client_id
        self._secret = client_secret
        self._endpoints = endpoints
        self._timeout = timeout
        self._max_bytes = max_response_bytes
        self._clock = clock
        self._sleep = sleep
        self._loopback = allow_loopback_http
        self._owner = object()
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout, trust_env=False, follow_redirects=False)

    async def __aenter__(self) -> OAuth2Client:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await _close_safely(self._http.aclose)

    async def _post(self, endpoint: str, data: dict[str, str], *, timeout: float | None = None) -> dict[str, Any]:
        import httpx

        data = {**data, "client_id": self._client_id}
        if self._secret:
            data["client_secret"] = self._secret
        try:
            async with asyncio.timeout(self._timeout if timeout is None else min(timeout, self._timeout)):
                request = self._http.build_request(
                    "POST", endpoint, data=data, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
                )
                response = await self._http.send(request, stream=True)
                try:
                    if (
                        response.is_redirect
                        or response.headers.get("content-encoding", "identity").strip().lower() != "identity"
                    ):
                        raise OAuth2ClientError("invalid_response")
                    content = bytearray()
                    if response.is_stream_consumed:
                        # Trusted custom transports may supply buffered responses.
                        # Their prior allocation is outside this payload cap.
                        if len(response.content) > self._max_bytes:
                            raise OAuth2ClientError("invalid_response")
                        content.extend(response.content)
                    else:
                        if not isinstance(response.stream, httpx.AsyncByteStream):
                            raise OAuth2ClientError("invalid_response")
                        # aiter_raw() auto-closes outside our cancellation shield.
                        async for chunk in response.stream:
                            if len(chunk) > self._max_bytes - len(content):
                                raise OAuth2ClientError("invalid_response")
                            content.extend(chunk)
                    doc = json.loads(content)
                    if not isinstance(doc, dict):
                        raise OAuth2ClientError("invalid_response")
                    if "error" in doc:
                        error = doc["error"]
                        raise OAuth2ClientError(error if isinstance(error, str) else "invalid_response")
                    if response.status_code != 200:
                        raise OAuth2ClientError("invalid_response")
                    return doc
                finally:
                    await _close_safely(response.aclose)
        except (httpx.TimeoutException, TimeoutError):
            raise OAuth2ClientError("timeout") from None
        except httpx.HTTPError:
            raise OAuth2ClientError("transport_error") from None
        except (ValueError, UnicodeError, RecursionError):
            raise OAuth2ClientError("invalid_response") from None

    @staticmethod
    def _tokens(doc: dict[str, Any]) -> OAuth2Tokens:
        access_token = _text(doc, "access_token")
        token_type = _text(doc, "token_type")
        if token_type.lower() != "bearer":
            raise OAuth2ClientError("invalid_response")
        return OAuth2Tokens(
            access_token=access_token,
            token_type="Bearer",
            expires_in=_positive(doc["expires_in"]) if "expires_in" in doc else None,
            refresh_token=_text(doc, "refresh_token") if "refresh_token" in doc else None,
            id_token=_text(doc, "id_token") if "id_token" in doc else None,
            scope=_text(doc, "scope") if "scope" in doc else None,
        )

    async def exchange_code(self, code: str, *, redirect_uri: str, code_verifier: str) -> OAuth2Tokens:
        """Exchange only after caller verifies callback state/issuer/URI and replay.

        The redirect URI is sent verbatim; no listener is created. Returned ID
        tokens are opaque and must not be used as authenticated identity.
        """
        pkce_challenge(code_verifier)
        if not code or not redirect_uri:
            raise ValueError("code and redirect_uri are required")
        doc = await self._post(
            self._endpoints.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return self._tokens(doc)

    async def authorize_device(self, *, scopes: tuple[str, ...] = ()) -> DeviceAuthorization:
        endpoint = self._endpoints.device_authorization_endpoint
        if endpoint is None:
            raise ValueError("No device authorization endpoint configured")
        started = self._clock()
        doc = await self._post(endpoint, {"scope": " ".join(scopes)} if scopes else {})
        verification_uri = _text(doc, "verification_uri")
        complete = _text(doc, "verification_uri_complete") if "verification_uri_complete" in doc else None
        try:
            _endpoint(verification_uri, self._loopback)
            if complete:
                _endpoint(complete, self._loopback)
        except ValueError:
            raise OAuth2ClientError("invalid_response") from None
        return DeviceAuthorization(
            device_code=_text(doc, "device_code"),
            user_code=_text(doc, "user_code"),
            verification_uri=verification_uri,
            verification_uri_complete=complete,
            expires_at=started + _positive(doc.get("expires_in")),
            interval=_positive(doc.get("interval", 5)),
            _owner=self._owner,
        )

    async def poll_device_token(self, grant: DeviceAuthorization) -> OAuth2Tokens:
        """Poll with cumulative slow_down, timeout backoff and a monotonic deadline."""
        if grant._owner is not self._owner:
            raise ValueError("Device grant belongs to another OAuth client")
        interval = grant.interval
        while True:
            remaining = grant.expires_at - self._clock()
            if remaining <= interval:
                raise OAuth2ClientError("expired_token")
            await self._sleep(interval)
            remaining = grant.expires_at - self._clock()
            if remaining <= 0:
                raise OAuth2ClientError("expired_token")
            try:
                doc = await self._post(
                    self._endpoints.token_endpoint,
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": grant.device_code,
                    },
                    timeout=remaining,
                )
                if self._clock() >= grant.expires_at:
                    raise OAuth2ClientError("expired_token")
                return self._tokens(doc)
            except OAuth2ClientError as exc:
                if exc.code == "slow_down":
                    interval += 5
                elif exc.code == "timeout":
                    interval *= 2
                elif exc.code != "authorization_pending":
                    raise
