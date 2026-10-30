# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Factory-owned SDK clients over borrowed native streaming HTTP ports."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self, cast

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider

from pyfly.agentic.models.spec import ModelClientPolicy, ModelSpec
from pyfly.client.ports.outbound import StreamingHttpClientPort


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: Any, context: Any) -> None:
        self.response, self.context = response, context
        self.closed = False
        self._iterator = self._chunks()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._iterator:
            yield chunk

    async def _chunks(self) -> AsyncIterator[bytes]:
        if self.response.is_stream_consumed:
            yield self.response.content
        else:
            async for chunk in self.response.aiter_raw():
                yield chunk

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            await self.context.__aexit__(None, None, None)


class _PortTransport(httpx.AsyncBaseTransport):
    def __init__(self, port: StreamingHttpClientPort, connect_timeout: float | None) -> None:
        self.port, self.connect_timeout = port, connect_timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = dict(request.headers)
        headers.pop("host", None)  # The native port owns any admitted Host override.
        kwargs: dict[str, Any] = {
            "headers": headers,
            "content": await request.aread(),
            "follow_redirects": False,
            "suppress_cookies": True,
        }
        timeout = request.extensions.get("timeout")
        if isinstance(timeout, dict):
            timeout = dict(timeout)
            if self.connect_timeout is not None:
                current = timeout.get("connect")
                timeout["connect"] = min(current, self.connect_timeout) if current is not None else self.connect_timeout
            kwargs["timeout"] = timeout
        context = self.port.stream(request.method, str(request.url), **kwargs)
        response = await context.__aenter__()
        try:
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=_ResponseStream(response, context),
                request=request,
            )
        except BaseException:
            await context.__aexit__(None, None, None)
            raise

    async def aclose(self) -> None:
        # The DI context owns the shared native port, not an SDK/model wrapper.
        pass


class _OwnedProvider(OpenAIProvider):
    def __init__(self, client: AsyncOpenAI) -> None:
        super().__init__(openai_client=client)
        self._sdk_entries = 0
        self._sdk_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        async with self._sdk_lock:
            await super().__aenter__()
            self._sdk_entries += 1
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> bool | None:
        async with self._sdk_lock:
            result = await super().__aexit__(exc_type, exc_val, exc_tb)
            if self._sdk_entries:
                self._sdk_entries -= 1
                if not self._sdk_entries:
                    await self.client.close()
            return result


class _OwnedOllamaProvider(_OwnedProvider):
    @property
    def name(self) -> str:
        return "ollama"

    @staticmethod
    def model_profile(model_name: str) -> ModelProfile | None:
        return OllamaProvider.model_profile(model_name)


def build_openai_model(
    spec: ModelSpec, secret: str | None, profile: ModelProfile | None, class_name: str, policy: ModelClientPolicy
) -> Model:
    base_url = spec.base_url
    provider_class = _OwnedProvider
    if spec.provider == "ollama":
        base_url = base_url or os.getenv("OLLAMA_BASE_URL")
        secret = secret or os.getenv("OLLAMA_API_KEY") or "api-key-not-set"
        provider_class = _OwnedOllamaProvider
    elif base_url and secret is None and not (os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY")):
        secret = "unused"
    kwargs: dict[str, Any] = {"base_url": base_url, "api_key": secret, "max_retries": policy.max_retries}
    if policy.timeout is not None:
        kwargs["timeout"] = policy.timeout
    if policy.http_client is not None:
        kwargs["http_client"] = httpx.AsyncClient(
            transport=_PortTransport(cast(StreamingHttpClientPort, policy.http_client), policy.connect_timeout),
            follow_redirects=False,
            trust_env=False,
        )
    provider = provider_class(AsyncOpenAI(**kwargs))
    model_class = OpenAIResponsesModel if class_name == "OpenAIResponsesModel" else OpenAIChatModel
    model_kwargs: dict[str, Any] = {"profile": profile} if profile is not None else {}
    return model_class(spec.model, provider=provider, **model_kwargs)
