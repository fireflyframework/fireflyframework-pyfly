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
"""pyfly flags: local (boots the app) and remote (--url, the actuator) modes (spec 6.2 CLI)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from click.testing import CliRunner

from pyfly.cli import _introspect, flags_cmds
from pyfly.cli._introspect import ActuatorClient
from pyfly.cli.flags_cmds import flags_group
from pyfly.core.application import pyfly_application

PYFLY_YAML = """
pyfly:
  feature-flags:
    enabled: true
    flags:
      kill: true
      theme:
        state: ENABLED
        variants: {"light": "light", "dark": "dark"}
        defaultVariant: "light"
    sources:
      store: {enabled: true, driver: memory}
    management:
      writes: true
"""


@pyfly_application(name="flags-cli-app")
class FlagsApp:
    pass


@pytest.fixture
def local_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    (tmp_path / "pyfly.yaml").write_text(PYFLY_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        flags_cmds, "run_in_context", lambda operation: _introspect.run_in_context(operation, app_class=FlagsApp)
    )
    # In-process CLI boots replace process-wide logging; unrelated tests must retain their own configuration.
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    structured = structlog.get_config()
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        structlog.configure(**structured)


def _json(args: list[str]) -> Any:
    result = CliRunner().invoke(flags_group, [*args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_list_and_show_locally(local_app: None) -> None:
    listing = _json(["list"])
    assert [flag["key"] for flag in listing["flags"]] == ["kill", "theme"]
    assert _json(["show", "theme"])["origin"] == "config"


def test_writes_locally_record_the_os_user(local_app: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "ana")
    detail = _json(["disable", "kill"])
    assert detail["definition"]["state"] == "DISABLED" and detail["history"][0]["actor"] == "cli:ana"
    assert _json(["default-variant", "theme", "dark"])["definition"]["defaultVariant"] == "dark"


def test_put_reads_a_definition_file(local_app: None, tmp_path: Path) -> None:
    definition = tmp_path / "kill.json"
    definition.write_text(
        json.dumps({"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "off"})
    )
    assert _json(["put", "kill", "--file", str(definition)])["definition"]["defaultVariant"] == "off"


def test_evaluate_with_a_context(local_app: None) -> None:
    result = _json(["evaluate", "theme", "--context", '{"plan": "pro"}', "--targeting-key", "u-1"])
    assert (result["value"], result["reason"]) == ("light", "STATIC")


def test_an_error_prints_the_code_and_exits_1(local_app: None) -> None:
    result = CliRunner().invoke(flags_group, ["default-variant", "theme", "neon"])
    assert result.exit_code == 1
    assert "unknown-variant" in result.output
    missing = CliRunner().invoke(flags_group, ["show", "missing"])
    assert missing.exit_code == 1 and "unknown-flag" in missing.output


class FakeActuator:
    calls: list[tuple[str, str, Any]] = []

    def __init__(self, base_url: str, **_: Any) -> None:
        self.base_url = base_url

    def get(self, endpoint: str, **_: Any) -> Any:
        FakeActuator.calls.append(("GET", endpoint, None))
        return {"flags": []}

    def post(self, endpoint: str, body: dict[str, Any]) -> Any:
        FakeActuator.calls.append(("POST", endpoint, body))
        if body["action"] == "delete":
            return {"error": "conflict", "message": "expected version 3"}
        return {"key": "kill", "definition": {}}


def test_remote_mode_uses_the_actuator(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeActuator.calls = []
    monkeypatch.setattr(flags_cmds, "ActuatorClient", FakeActuator)
    url = ["--url", "http://svc:9090"]
    assert CliRunner().invoke(flags_group, ["list", *url]).exit_code == 0
    assert CliRunner().invoke(flags_group, ["enable", "kill", "--expected-version", "2", *url]).exit_code == 0
    failed = CliRunner().invoke(flags_group, ["delete", "kill", *url])
    assert failed.exit_code == 1 and "conflict" in failed.output
    assert FakeActuator.calls == [
        ("GET", "flags", None),
        ("POST", "flags/kill", {"action": "enable", "expectedVersion": 2}),
        ("POST", "flags/kill", {"action": "delete"}),
    ]


def test_actuator_client_post_returns_error_bodies_and_fails_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/broken"):
            return httpx.Response(500, text="boom")
        return httpx.Response(400, json={"error": "conflict", "message": "x", "sent": json.loads(request.content)})

    client = ActuatorClient("http://svc", transport=httpx.MockTransport(handler))
    assert client.post("flags/kill", {"action": "delete"}) == {
        "error": "conflict",
        "message": "x",
        "sent": {"action": "delete"},
    }
    with pytest.raises(SystemExit):
        client.post("flags/broken", {"action": "delete"})


def test_the_cli_starts_without_openfeature() -> None:
    """Review focus 4: `pyfly` imports flags_cmds on every invocation, extra or not."""
    code = (
        "import sys; sys.modules['openfeature'] = None\n"
        "from click.testing import CliRunner\n"
        "from pyfly.cli.main import cli\n"
        "result = CliRunner().invoke(cli, ['flags', '--help'])\n"
        "print(result.exit_code, 'enable' in result.output)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 True"


def test_json_refusals_stay_machine_readable(local_app: None) -> None:
    result = CliRunner().invoke(flags_group, ["show", "missing", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "unknown-flag"


def test_remote_show_preserves_the_portable_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ActuatorClient(
        "http://svc",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={"error": "unknown-flag", "message": "absent"})
        ),
    )
    monkeypatch.setattr(flags_cmds, "ActuatorClient", lambda url: client)
    result = CliRunner().invoke(flags_group, ["show", "missing", "--url", "http://svc", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "unknown-flag", "message": "absent"}


@pytest.mark.parametrize("status", [401, 403])
def test_remote_show_reports_non_json_refusal_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    client = ActuatorClient(
        "http://svc", transport=httpx.MockTransport(lambda request: httpx.Response(status, text="Access denied"))
    )
    monkeypatch.setattr(flags_cmds, "ActuatorClient", lambda url: client)
    result = CliRunner().invoke(flags_group, ["show", "kill", "--url", "http://svc", "--json"])
    assert result.exit_code == 1
    assert str(status) in result.output and "without JSON" in result.output
    assert "unknown-flag" not in result.output
    assert not isinstance(result.exception, ValueError)


def test_legacy_introspection_get_still_reports_http_status() -> None:
    client = ActuatorClient(
        "http://svc", transport=httpx.MockTransport(lambda request: httpx.Response(401, text="Access denied"))
    )
    with pytest.raises(SystemExit, match="1"):
        client.get("health")


@pytest.mark.parametrize("contents, cause", [(None, "No such file"), (b"\xff", "codec")])
def test_put_reports_unreadable_definition_file(tmp_path: Path, contents: bytes | None, cause: str) -> None:
    path = tmp_path / "definition.json"
    if contents is not None:
        path.write_bytes(contents)
    result = CliRunner().invoke(flags_group, ["put", "kill", "--file", str(path), "--url", "http://svc"])
    assert result.exit_code == 1
    assert str(path) in result.output and cause in result.output
    assert not isinstance(result.exception, (OSError, UnicodeError))


def test_pending_receipts_remain_successful_and_explain_visibility(
    local_app: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyfly.feature_flags.registry import FlagRegistry

    async def failed_refresh(self: Any, name: str) -> list[str]:
        raise OSError("store unavailable for local refresh")

    monkeypatch.setattr(FlagRegistry, "refresh", failed_refresh)
    path = tmp_path / "new.json"
    path.write_text(json.dumps({"state": "ENABLED", "variants": {"on": True}, "defaultVariant": "on"}))
    result = CliRunner().invoke(flags_group, ["put", "new", "--file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"key": "new", "refreshPending": True}
    text = CliRunner().invoke(flags_group, ["put", "new", "--file", str(path)])
    assert text.exit_code == 0 and "local refresh is pending" in text.output


def test_local_commands_use_a_persistent_database_across_boots(local_app: None, tmp_path: Path) -> None:
    import yaml

    config = yaml.safe_load(PYFLY_YAML)
    config["pyfly"]["data"] = {"relational": {"enabled": True, "url": f"sqlite+aiosqlite:///{tmp_path / 'flags.db'}"}}
    config["pyfly"]["feature-flags"]["sources"]["store"]["driver"] = "database"
    (tmp_path / "pyfly.yaml").write_text(yaml.safe_dump(config))
    assert _json(["disable", "kill", "--expected-version", "0"])["version"] == 1
    assert _json(["show", "kill"])["definition"]["state"] == "DISABLED"
    assert _json(["enable", "kill", "--expected-version", "1"])["version"] == 2
    conflict = CliRunner().invoke(flags_group, ["disable", "kill", "--expected-version", "1", "--json"])
    assert conflict.exit_code == 1 and json.loads(conflict.output)["error"] == "conflict"
    reverted = _json(["delete", "kill", "--expected-version", "2"])
    assert reverted["origin"] == "config" and reverted["version"] is None
    assert [change["action"] for change in reverted["history"]] == ["delete", "put", "put"]


@pytest.mark.parametrize("fails", [False, True])
def test_run_in_context_owns_one_loop_and_always_shuts_down(monkeypatch: pytest.MonkeyPatch, fails: bool) -> None:
    import asyncio

    from pyfly.core import application

    seen = []

    class App:
        context = object()

        def __init__(self, cls: type) -> None:
            pass

        async def startup(self) -> None:
            seen.append(("start", asyncio.get_running_loop()))

        async def shutdown(self) -> None:
            seen.append(("stop", asyncio.get_running_loop()))

    async def operation(context: Any) -> int:
        assert context is App.context
        seen.append(("run", asyncio.get_running_loop()))
        if fails:
            raise ValueError("operation failed")
        return 42

    monkeypatch.setattr(application, "PyFlyApplication", App)
    if fails:
        with pytest.raises(ValueError, match="operation failed"):
            _introspect.run_in_context(operation, app_class=FlagsApp)
    else:
        assert _introspect.run_in_context(operation, app_class=FlagsApp) == 42
    assert [name for name, loop in seen] == ["start", "run", "stop"]
    assert len({loop for name, loop in seen}) == 1
