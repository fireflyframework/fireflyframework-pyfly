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
"""'pyfly flags' — list, show, change and evaluate feature flags.

Offline (default) the command boots the application and works on its ``FlagManagement`` (writes are recorded as
``cli:<os-user>``); with ``--url`` it talks to the running application's ``/actuator/flags`` endpoint. Nothing
here imports the feature-flag modules until a command runs, so ``pyfly`` starts without the ``feature-flags``
extra.
"""

from __future__ import annotations

import getpass
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn

import click

from pyfly.cli._introspect import ActuatorClient, run_in_context
from pyfly.cli.console import err_console
from pyfly.cli.introspect_cmds import _emit, _url_option

__all__ = ["flags_group"]


def _fail(code: str, message: str, *, as_json: bool = False) -> NoReturn:
    if as_json:
        click.echo(json.dumps({"error": code, "message": message}))
    else:
        err_console.print(f"[error]✗[/error] {code}: {message}")
    raise SystemExit(1)


def _local(operation: Callable[[Any], Awaitable[Any]]) -> Any:
    async def run(context: Any) -> Any:
        from pyfly.feature_flags.management import FlagManagement, FlagManagementError

        try:
            management = context.get_bean(FlagManagement)
        except Exception:  # noqa: BLE001 — NoSuchBeanError: the subsystem is off
            return {"error": "not-writable", "message": "feature flags are not enabled (pyfly.feature-flags.enabled)"}
        try:
            return await operation(management)
        except FlagManagementError as error:
            return error.to_body()

    return run_in_context(run)


def _show(result: Any, *, as_json: bool, title: str) -> None:
    if isinstance(result, dict) and "error" in result:
        _fail(str(result["error"]), str(result.get("message", "")), as_json=as_json)
    if isinstance(result, dict) and result.get("refreshPending") is True and not as_json:
        click.echo("Write accepted; local refresh is pending. Poll the flag for visibility.")
    _emit(result, as_json=as_json, title=title)


def _execute(key: str, body: dict[str, Any], url: str | None, as_json: bool) -> None:
    if url:
        result = ActuatorClient(url).post(f"flags/{key}", body)
    else:
        actor = f"cli:{getpass.getuser()}"
        result = _local(lambda management: management.execute(key, body, actor=actor))
    _show(result, as_json=as_json, title=f"flags/{key}")


def _with_version(body: dict[str, Any], expected_version: int | None) -> dict[str, Any]:
    return body if expected_version is None else {**body, "expectedVersion": expected_version}


_expected_version = click.option(
    "--expected-version", type=int, default=None, help="Fail with 'conflict' unless the stored version is this."
)


@click.group("flags")
def flags_group() -> None:
    """Inspect and change feature flags (offline, or a running app with --url)."""


@flags_group.command("list")
@_url_option
def list_cmd(url: str | None, as_json: bool) -> None:
    """List the provider, the sources and every flag."""
    result = (
        ActuatorClient(url).get("flags", allow_error_body=True)
        if url
        else _local(lambda management: management.overview())
    )
    _show(result, as_json=as_json, title="Feature flags")


@flags_group.command("show")
@click.argument("key")
@_url_option
def show_cmd(key: str, url: str | None, as_json: bool) -> None:
    """Show one flag: its definition, every layer, its version and its history."""
    if url:
        result = ActuatorClient(url).get(f"flags/{key}", allow_error_body=True)
    else:
        result = _local(lambda management: management.detail(key))
        if result is None:
            _fail("unknown-flag", f"no layer defines {key!r}", as_json=as_json)
    _show(result, as_json=as_json, title=f"flags/{key}")


@flags_group.command("enable")
@click.argument("key")
@_expected_version
@_url_option
def enable_cmd(key: str, expected_version: int | None, url: str | None, as_json: bool) -> None:
    """Enable a flag (copied into the store when only a lower layer defines it)."""
    _execute(key, _with_version({"action": "enable"}, expected_version), url, as_json)


@flags_group.command("disable")
@click.argument("key")
@_expected_version
@_url_option
def disable_cmd(key: str, expected_version: int | None, url: str | None, as_json: bool) -> None:
    """Disable a flag: evaluations return the caller's default."""
    _execute(key, _with_version({"action": "disable"}, expected_version), url, as_json)


@flags_group.command("default-variant")
@click.argument("key")
@click.argument("variant")
@_expected_version
@_url_option
def default_variant_cmd(key: str, variant: str, expected_version: int | None, url: str | None, as_json: bool) -> None:
    """Set a flag's default variant."""
    _execute(key, _with_version({"action": "default-variant", "variant": variant}, expected_version), url, as_json)


@flags_group.command("put")
@click.argument("key")
@click.option("--file", "path", required=True, help="A JSON flagd definition ('-' reads stdin).")
@_expected_version
@_url_option
def put_cmd(key: str, path: str, expected_version: int | None, url: str | None, as_json: bool) -> None:
    """Write a whole definition to the store."""
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        definition = json.loads(text)
    except ValueError as error:
        _fail("bad-request", f"the definition is not JSON: {error}", as_json=as_json)
    _execute(key, _with_version({"action": "put", "definition": definition}, expected_version), url, as_json)


@flags_group.command("delete")
@click.argument("key")
@_expected_version
@_url_option
def delete_cmd(key: str, expected_version: int | None, url: str | None, as_json: bool) -> None:
    """Delete the store's definition: the next layer applies again."""
    _execute(key, _with_version({"action": "delete"}, expected_version), url, as_json)


@flags_group.command("evaluate")
@click.argument("key")
@click.option("--context", "context_json", default=None, help='The evaluation context as JSON: {"plan": "pro"}.')
@click.option("--targeting-key", default=None, help="The targeting key.")
@_url_option
def evaluate_cmd(key: str, context_json: str | None, targeting_key: str | None, url: str | None, as_json: bool) -> None:
    """Preview an evaluation (no metric, no exposure event)."""
    body: dict[str, Any] = {"action": "evaluate"}
    if context_json:
        try:
            body["context"] = json.loads(context_json)
        except ValueError as error:
            _fail("bad-request", f"--context is not JSON: {error}", as_json=as_json)
    if targeting_key:
        body["targetingKey"] = targeting_key
    _execute(key, body, url, as_json)
