# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native configuration and public launcher delegation without starting a server."""

from __future__ import annotations

import os
from typing import Any

import click
import pytest
from click.testing import CliRunner

from pyfly.cli.run import _load_server_properties, run_command


@pytest.fixture(autouse=True)
def restore_launch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "_PYFLY_BANNER_PRINTED",
        "_PYFLY_WORKERS",
        "_PYFLY_SERVER_HOST",
        "_PYFLY_SERVER_PORT",
        "_PYFLY_SERVER_TYPE",
        "_PYFLY_EVENT_LOOP",
        "_PYFLY_HTTP",
        "PYFLY_SERVER_EVENT_LOOP",
        "PYFLY_SERVER_GRACEFUL_TIMEOUT",
    ):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.delenv("PYFLY_SERVER_EVENT_LOOP", raising=False)
    monkeypatch.delenv("PYFLY_SERVER_GRACEFUL_TIMEOUT", raising=False)


def test_server_binding_keeps_profiles_environment_and_nested_properties(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyfly.yaml").write_text(
        "pyfly:\n  server:\n    event-loop: asyncio\n    http: h2\n    graceful-timeout: 17\n"
        "    granian:\n      runtime-threads: 3\n    observability:\n      access-log: true\n"
    )
    (tmp_path / "pyfly-prod.yaml").write_text("pyfly:\n  server:\n    graceful-timeout: 29\n")
    monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", "prod")
    monkeypatch.setenv("PYFLY_SERVER_GRACEFUL_TIMEOUT", "47")
    properties = _load_server_properties()
    assert properties.event_loop == "asyncio" and properties.http == "h2"
    assert properties.graceful_timeout == 47
    assert properties.granian.runtime_threads == 3 and properties.observability.access_log


def test_invalid_typed_configuration_is_not_silently_replaced(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyfly.yaml").write_text("pyfly:\n  server:\n    port: invalid\n")
    with pytest.raises(click.BadParameter):
        _load_server_properties()


def test_cli_delegates_complete_bound_configuration_to_public_launcher(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module("pyfly.cli.run")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(module, "launch", lambda app, properties, **ports: calls.append((app, properties, ports)))
    monkeypatch.setattr(
        module,
        "_create_server_adapter",
        lambda kind: type(
            "Server",
            (),
            {
                "server_info": type("Info", (), {"name": "uvicorn"})(),
            },
        )(),
    )
    monkeypatch.setattr(module, "_create_event_loop_adapter", lambda kind: object())
    monkeypatch.setattr(module, "_print_cli_banner", lambda *args: None)
    result = CliRunner().invoke(
        run_command,
        [
            "--app",
            "missing:app",
            "--port",
            "18881",
            "--server",
            "uvicorn",
            "-D",
            "server.event-loop=asyncio",
            "-D",
            "server.graceful-timeout=41",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    app, properties, ports = calls[0]
    assert app == "missing:app" and properties.port == 18881
    assert properties.graceful_timeout == 41 and properties.event_loop == "asyncio"
    assert "server" in ports and "event_loop" in ports


def test_cli_keeps_banner_worker_and_multiprocess_preparation_before_launch(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    module = importlib.import_module("pyfly.cli.run")
    multiprocess = importlib.import_module("pyfly.observability.multiprocess")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(module, "_print_cli_banner", lambda *args: calls.append("banner"))
    monkeypatch.setattr(
        module,
        "_create_server_adapter",
        lambda kind: type(
            "Server",
            (),
            {
                "server_info": type("Info", (), {"name": "uvicorn"})(),
            },
        )(),
    )
    monkeypatch.setattr(module, "_create_event_loop_adapter", lambda kind: object())
    monkeypatch.setattr(multiprocess, "init_multiprocess_dir", lambda workers: calls.append(("multiprocess", workers)))
    monkeypatch.setattr(module, "launch", lambda app, properties, **ports: calls.append(("launch", properties.workers)))
    result = CliRunner().invoke(run_command, ["--app", "missing:app", "--workers", "3"])
    assert result.exit_code == 0, result.output
    assert calls == ["banner", ("multiprocess", 3), ("launch", 3)]
    assert os.environ["_PYFLY_BANNER_PRINTED"] == "1" and os.environ["_PYFLY_WORKERS"] == "3"


def test_reload_keeps_its_uvicorn_path_and_bound_listener(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    module = importlib.import_module("pyfly.cli.run")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYFLY_SERVER_HOST", "127.0.0.1")
    calls = []
    monkeypatch.setattr(module, "_run_with_uvicorn_reload", lambda *args: calls.append(args))
    monkeypatch.setattr(module, "launch", lambda *args, **kwargs: pytest.fail("reload launched a second server"))
    result = CliRunner().invoke(run_command, ["--app", "missing:app", "--reload", "--port", "18882"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][:4] == ("missing:app", "127.0.0.1", 18882, [])
    assert calls[0][4].host == "127.0.0.1" and calls[0][4].port == 18882
