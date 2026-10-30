# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Exercise native launch configuration, replacing only network runners."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from pyfly.config.properties.server import ServerProperties
from pyfly.server import launch, validate_server_properties


class FakeLoop:
    name = "asyncio"

    def __init__(self) -> None:
        self.installed = False

    def install(self) -> None:
        self.installed = True


@pytest.fixture(autouse=True)
def isolate_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SERVER_HOST", "SERVER_PORT", "SERVER_TYPE", "EVENT_LOOP", "HTTP"):
        monkeypatch.setenv("_PYFLY_" + name, "")


def test_reload_preserves_tls_and_native_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("pyfly.cli.run")
    uvicorn = importlib.import_module("uvicorn")
    monkeypatch.chdir(tmp_path)
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    certificate.write_text("synthetic certificate")
    key.write_text("synthetic key")
    (tmp_path / "pyfly.yaml").write_text(
        f"pyfly:\n  server:\n    ssl-certfile: {certificate}\n    ssl-keyfile: {key}\n"
        "    backlog: 2048\n    keep-alive-timeout: 19\n    graceful-timeout: 37\n"
        "    max-concurrent-connections: 53\n    max-requests-per-worker: 71\n"
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(module.run_command, ["--app", "never_import:app", "--reload"])
    assert result.exit_code == 0, result.exception
    assert calls[0]["ssl_certfile"] == str(certificate)
    assert calls[0]["ssl_keyfile"] == str(key)
    assert calls[0]["backlog"] == 2048
    assert calls[0]["timeout_keep_alive"] == 19
    assert calls[0]["timeout_graceful_shutdown"] == 37
    assert calls[0]["limit_concurrency"] == 53
    assert calls[0]["limit_max_requests"] == 71


def test_cli_and_application_share_file_selected_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.cli.run import _load_server_properties
    from pyfly.core.application import PyFlyApplication

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYFLY_PROFILES_ACTIVE", raising=False)
    (tmp_path / "pyfly.yaml").write_text("pyfly:\n  profiles:\n    active: prod\n  server:\n    port: 18080\n")
    (tmp_path / "pyfly-prod.yaml").write_text("pyfly:\n  server:\n    port: 18443\n")
    assert PyFlyApplication._resolve_profiles_early(tmp_path) == ["prod"]
    assert _load_server_properties().port == 18443


def test_explicit_loop_reaches_uvicorn_without_mutating_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

    uvicorn = importlib.import_module("uvicorn")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    properties, loop = ServerProperties(), FakeLoop()
    launch("never_import:app", properties, server=UvicornServerAdapter(), event_loop=loop)
    assert loop.installed
    assert calls[0]["loop"] == "asyncio"
    assert properties.event_loop == "auto"


def test_uvicorn_preserves_ephemeral_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

    uvicorn = importlib.import_module("uvicorn")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    launch("never_import:app", ServerProperties(port=0), server=UvicornServerAdapter(), event_loop=FakeLoop())
    assert calls[0]["port"] == 0


def test_async_uvicorn_preserves_ephemeral_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

    uvicorn = importlib.import_module("uvicorn")
    calls: list[dict[str, Any]] = []

    async def fake_serve() -> None:
        return None

    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(uvicorn, "Server", lambda config: SimpleNamespace(serve=fake_serve))
    coroutine = UvicornServerAdapter().serve_async("never_import:app", ServerProperties(port=0))
    with pytest.raises(StopIteration):
        coroutine.send(None)
    assert calls[0]["port"] == 0


def test_granian_preserves_ephemeral_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server.adapters.granian.adapter import GranianServerAdapter

    calls: list[dict[str, Any]] = []
    granian, constants = ModuleType("granian"), ModuleType("granian.constants")
    granian.Granian = lambda **kwargs: calls.append(kwargs) or SimpleNamespace(serve=lambda: None)  # type: ignore[attr-defined]
    constants.Interfaces = SimpleNamespace(ASGI="asgi")  # type: ignore[attr-defined]
    constants.HTTPModes = SimpleNamespace(auto="auto", http1="1", http2="2")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "granian", granian)
    monkeypatch.setitem(sys.modules, "granian.constants", constants)
    launch("never_import:app", ServerProperties(port=0), server=GranianServerAdapter(), event_loop=FakeLoop())
    assert calls[0]["port"] == 0


def test_hypercorn_preserves_ephemeral_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server.adapters.hypercorn.adapter import HypercornServerAdapter

    calls: list[Any] = []
    runtime, configuration = ModuleType("hypercorn.asyncio"), ModuleType("hypercorn.config")

    async def fake_serve(app: Any, config: Any, **kwargs: Any) -> None:
        calls.append(config)

    runtime.serve = fake_serve  # type: ignore[attr-defined]
    configuration.Config = SimpleNamespace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hypercorn.asyncio", runtime)
    monkeypatch.setitem(sys.modules, "hypercorn.config", configuration)
    coroutine = HypercornServerAdapter().serve_async("never_import:app", ServerProperties(port=0))
    with pytest.raises(StopIteration):
        coroutine.send(None)
    assert calls[0].bind == ["0.0.0.0:0"]


def test_tls_stat_failure_uses_safe_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def inaccessible(path: Path) -> bool:
        raise OSError("private credential path")

    monkeypatch.setattr(Path, "is_file", inaccessible)
    with pytest.raises(ValueError, match="TLS certificate and key files must exist") as error:
        validate_server_properties(ServerProperties(ssl_certfile="cert.pem", ssl_keyfile="key.pem"))
    assert "private credential path" not in str(error.value)


def test_winloop_has_a_valid_native_uvicorn_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

    uvicorn = importlib.import_module("uvicorn")
    winloop = ModuleType("winloop")

    def new_event_loop() -> Any:
        pytest.fail("Configuration inspection must not open an event loop")

    winloop.new_event_loop = new_event_loop  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winloop", winloop)
    loop = FakeLoop()
    loop.name = "winloop"
    calls = []

    def inspect_runner(app: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        assert uvicorn.Config(app, **kwargs).get_loop_factory() is new_event_loop

    monkeypatch.setattr(uvicorn, "run", inspect_runner)
    launch("never_import:app", ServerProperties(), server=UvicornServerAdapter(), event_loop=loop)
    assert calls[0]["loop"] == "winloop:new_event_loop"


def test_reload_rejects_unavailable_explicit_loop_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("pyfly.cli.run")
    uvicorn = importlib.import_module("uvicorn")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYFLY_SERVER_EVENT_LOOP", "")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        module.run_command,
        ["--app", "never_import:app", "--reload", "-D", "server.event-loop=uvloop"],
    )
    assert result.exit_code != 0
    assert calls == []
