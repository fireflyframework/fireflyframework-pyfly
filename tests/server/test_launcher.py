# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Only fake ports; no serving, event-loop policy changes or application imports."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from pyfly.config.properties.server import ServerProperties


@pytest.fixture(autouse=True)
def restore_launch_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SERVER_HOST", "SERVER_PORT", "SERVER_TYPE", "EVENT_LOOP", "HTTP"):
        key = "_PYFLY_" + name
        monkeypatch.setenv(key, os.environ.get(key, ""))


class FakeLoop:
    name = "asyncio"

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def install(self) -> None:
        self.events.append("install")


class FakeServer:
    server_info = SimpleNamespace(name="test-server")

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def serve(self, app: Any, properties: ServerProperties) -> None:
        self.events.append((app, properties))


def test_launch_honors_explicit_ports_before_serving_full_properties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from pyfly.server import launcher

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    properties = ServerProperties(
        host="127.0.0.1",
        port=18082,
        http="h2",
        graceful_timeout=43,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        backlog=2048,
    )
    properties.granian.runtime_threads = 3
    properties.observability.access_log = True
    events: list[Any] = []
    server, loop = FakeServer(events), FakeLoop(events)
    monkeypatch.setattr(launcher, "select_server", lambda kind: pytest.fail("explicit server ignored"))
    monkeypatch.setattr(launcher, "select_event_loop", lambda kind: pytest.fail("explicit loop ignored"))
    launcher.launch("missing.application:app", properties, server=server, event_loop=loop)
    assert events[0] == "install" and events[1][0] == "missing.application:app"
    assert events[1][1].event_loop == "asyncio"
    assert properties.event_loop == "auto"
    assert properties.granian.runtime_threads == 3 and properties.observability.access_log
    assert properties.http == "h2" and properties.graceful_timeout == 43
    assert os.environ["_PYFLY_SERVER_PORT"] == "18082"


def test_launch_uses_native_selection_when_no_ports_are_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server import launcher

    events: list[Any] = []
    server, loop = FakeServer(events), FakeLoop(events)
    monkeypatch.setattr(launcher, "select_server", lambda kind: server if kind == "auto" else None)
    monkeypatch.setattr(launcher, "select_event_loop", lambda kind: loop if kind == "auto" else None)
    properties = ServerProperties()
    launcher.launch("missing:app", properties)
    assert events[0] == "install" and events[1][0] == "missing:app"
    assert events[1][1].event_loop == "asyncio"
    assert properties.event_loop == "auto"


@pytest.mark.parametrize("kind", ["", "unsupported"])
def test_public_selectors_reject_unknown_names(kind: str) -> None:
    from pyfly.server import select_event_loop, select_server

    with pytest.raises(ValueError):
        select_server(kind)
    with pytest.raises(ValueError):
        select_event_loop(kind)


def test_explicit_unavailable_optional_adapter_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server import launcher

    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ValueError, match="installed"):
        launcher.select_server("granian")
    with pytest.raises(ValueError, match="installed"):
        launcher.select_event_loop("uvloop")


@pytest.mark.parametrize(
    "properties",
    [
        ServerProperties(ssl_certfile="only-cert.pem"),
        ServerProperties(ssl_certfile="missing-cert.pem", ssl_keyfile="missing-key.pem"),
        ServerProperties(port=70000),
    ],
)
def test_invalid_configuration_fails_before_install_or_serve(properties: ServerProperties) -> None:
    from pyfly.server import launch

    events: list[Any] = []
    with pytest.raises(ValueError):
        launch("missing:app", properties, server=FakeServer(events), event_loop=FakeLoop(events))
    assert events == []


def test_auto_selection_preserves_installed_adapter_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server import launcher

    events: list[Any] = []
    server, loop = FakeServer(events), FakeLoop(events)
    monkeypatch.setattr(
        launcher.importlib.util, "find_spec", lambda name: object() if name in {"uvicorn", "winloop"} else None
    )
    modules = {
        "pyfly.server.adapters.uvicorn.adapter": SimpleNamespace(UvicornServerAdapter=lambda: server),
        "pyfly.server.adapters.event_loop.winloop_adapter": SimpleNamespace(WinloopEventLoopAdapter=lambda: loop),
    }
    monkeypatch.setattr(launcher.importlib, "import_module", modules.__getitem__)
    assert launcher.select_server() is server
    assert launcher.select_event_loop() is loop
    assert events == []


def test_install_failure_propagates_without_serving() -> None:
    from pyfly.server import launch

    events: list[Any] = []

    class FailedLoop(FakeLoop):
        def install(self) -> None:
            raise RuntimeError("policy unavailable")

    with pytest.raises(RuntimeError, match="policy unavailable"):
        launch("missing:app", ServerProperties(), server=FakeServer(events), event_loop=FailedLoop(events))
    assert events == []
