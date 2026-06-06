# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""WebSocket adapter lifecycle tests (v26.06.19).

The websocket module previously had no tests. These lock in the registrar's
endpoint lifecycle and the v26.06.19 fixes: ``on_disconnect`` runs only when the
connection was accepted, its failures are logged (not silently swallowed), the
``WebSocketSession.accepted`` flag, message flow, and the documented contract
that ``on_message`` is NOT auto-dispatched by the framework.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from pyfly.websocket import WebSocketSession
from pyfly.websocket.adapters.starlette import WebSocketRegistrar


class _FakeRawWS:
    def __init__(self, incoming: tuple[str, ...] = ()) -> None:
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self.accepted = False
        self.path_params: dict[str, Any] = {}

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        raise WebSocketDisconnect(1000)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        pass


class _FakeCtx:
    def __init__(self, instance: Any) -> None:
        self._instance = instance
        self.container = type("_C", (), {"_registrations": {}})()

    def get_bean(self, cls: type) -> Any:
        return self._instance


def _endpoint(instance: Any, method_name: str = "chat") -> Any:
    return WebSocketRegistrar._make_lazy_handler(_FakeCtx(instance), type(instance), method_name)


class _EchoController:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def chat(self, session: WebSocketSession) -> None:
        await session.accept()
        self.events.append("accept")
        while True:
            msg = await session.receive_text()
            await session.send_text(f"echo:{msg}")

    async def on_disconnect(self, session: WebSocketSession) -> None:
        self.events.append("disconnect")


class _NoAcceptController:
    def __init__(self) -> None:
        self.disconnected = False

    async def chat(self, session: WebSocketSession) -> None:
        raise RuntimeError("boom before accept")

    async def on_disconnect(self, session: WebSocketSession) -> None:
        self.disconnected = True


class _BadCleanupController:
    async def chat(self, session: WebSocketSession) -> None:
        await session.accept()  # returns immediately

    async def on_disconnect(self, session: WebSocketSession) -> None:
        raise RuntimeError("cleanup failed")


class _OnMessageController:
    def __init__(self) -> None:
        self.on_message_calls = 0

    async def chat(self, session: WebSocketSession) -> None:
        await session.accept()
        try:
            while True:
                await session.receive_text()
        except WebSocketDisconnect:
            pass

    async def on_message(self, session: WebSocketSession, data: str) -> None:
        self.on_message_calls += 1  # must never be auto-invoked


@pytest.mark.asyncio
async def test_message_flow_and_disconnect_cleanup() -> None:
    ctrl = _EchoController()
    raw = _FakeRawWS(["hi", "there"])
    await _endpoint(ctrl)(raw)
    assert raw.sent == ["echo:hi", "echo:there"]  # messages flowed
    assert ctrl.events == ["accept", "disconnect"]  # on_disconnect ran after accept


@pytest.mark.asyncio
async def test_on_disconnect_not_called_when_never_accepted() -> None:
    ctrl = _NoAcceptController()
    await _endpoint(ctrl)(_FakeRawWS())  # handler errors before accept; must not raise
    assert ctrl.disconnected is False  # gated on session.accepted


@pytest.mark.asyncio
async def test_on_disconnect_error_is_logged_not_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfly.websocket.adapters.starlette"):
        await _endpoint(_BadCleanupController())(_FakeRawWS())  # must not raise
    assert any("on_disconnect" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_on_message_is_not_auto_dispatched() -> None:
    ctrl = _OnMessageController()
    await _endpoint(ctrl)(_FakeRawWS(["a", "b"]))
    assert ctrl.on_message_calls == 0  # framework never dispatches to on_message


@pytest.mark.asyncio
async def test_session_accepted_flag() -> None:
    session = WebSocketSession(_FakeRawWS())
    assert session.accepted is False
    await session.accept()
    assert session.accepted is True
