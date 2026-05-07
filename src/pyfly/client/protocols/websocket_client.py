# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""WebSocket client — wraps the third-party ``websockets`` library."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


class WebSocketClient:
    """Thin async WebSocket helper."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        ping_interval: float | None = 20.0,
        ping_timeout: float | None = 20.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

    async def connect(self) -> Any:
        try:
            import websockets  # type: ignore[import-not-found]
        except ImportError as exc:  # noqa: BLE001
            msg = "WebSocketClient requires websockets — `pip install websockets`"
            raise ImportError(msg) from exc
        return await websockets.connect(
            self._url,
            additional_headers=list(self._headers.items()),
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
        )

    async def stream(self, send: list[str] | None = None) -> AsyncIterator[Any]:
        connection = await self.connect()
        try:
            for message in send or []:
                await connection.send(message)
            async for message in connection:
                yield message
        finally:
            await connection.close()


@dataclass
class WebSocketClientBuilder:
    url: str = ""
    headers: dict[str, str] | None = None
    ping_interval: float | None = 20.0

    def with_url(self, value: str) -> WebSocketClientBuilder:
        self.url = value
        return self

    def with_header(self, name: str, value: str) -> WebSocketClientBuilder:
        if self.headers is None:
            self.headers = {}
        self.headers[name] = value
        return self

    def build(self) -> WebSocketClient:
        if not self.url:
            msg = "WebSocketClientBuilder requires a url"
            raise ValueError(msg)
        return WebSocketClient(url=self.url, headers=self.headers, ping_interval=self.ping_interval)
