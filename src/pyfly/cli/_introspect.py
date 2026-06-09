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
"""Shared helpers for CLI introspection: offline context boot + remote actuator client."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import os
import sys
from typing import Any

from pyfly.cli.console import err_console


def run_async(coro: Any) -> Any:
    """Drive a coroutine from a synchronous Click command."""
    return asyncio.run(coro)


def _discover_app_class() -> type:
    """Discover and import the @pyfly_application class from the current project."""
    from pyfly.cli.run import _discover_app, _ensure_src_on_path

    _ensure_src_on_path()
    app_path = _discover_app()
    if app_path is None:
        err_console.print("[error]✗[/error] No application found. Run inside a PyFly project or pass --url.")
        raise SystemExit(1)
    module_name = app_path.split(":")[0]
    module = importlib.import_module(module_name)
    for value in vars(module).values():
        if isinstance(value, type) and getattr(value, "__pyfly_application__", False):
            return value
    err_console.print(f"[error]✗[/error] No @pyfly_application class found in {module_name}.")
    raise SystemExit(1)


@contextlib.contextmanager
def _quiet_startup_env() -> Any:
    """Suppress the banner + startup logs DURING boot, restoring process env after.

    The framework reads these env vars while configuring logging at startup; we set
    them only for the duration of the boot and then restore the prior values, so a
    short-lived CLI invocation never leaks global state into the rest of the process
    (which would, e.g., break logging tests that boot a context).
    """
    quiet = {"_PYFLY_BANNER_PRINTED": "1", "PYFLY_LOGGING_LEVEL_ROOT": "CRITICAL"}
    saved = {k: os.environ.get(k) for k in quiet}
    for key, value in quiet.items():
        os.environ.setdefault(key, value)  # respect a user's explicit override
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def boot_context(*, app_class: type | None = None) -> Any:
    """Boot the application context offline (no HTTP server) and return it.

    Startup runs *quietly* (banner suppressed, logs silenced, stdout captured) so a
    command's ``--json`` output stays clean and pipeable (e.g. ``pyfly routes
    --json | jq``). Captured startup output is only surfaced if startup fails.
    """
    from pyfly.core.application import PyFlyApplication

    cls = app_class or _discover_app_class()
    captured = io.StringIO()
    try:
        # The env must be set BEFORE constructing PyFlyApplication — the constructor
        # configures logging from the env, so the quiet level has to be in place
        # before then (not just before startup()).
        with _quiet_startup_env(), contextlib.redirect_stdout(captured):
            app = PyFlyApplication(cls)
            run_async(app.startup())
    except Exception:
        # On failure, replay whatever startup printed so the error has context.
        sys.stdout.write(captured.getvalue())
        raise
    return app.context


class ActuatorClient:
    """Minimal sync client for a running app's ``/actuator/*`` endpoints."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def get(self, endpoint: str) -> Any:
        try:
            import httpx
        except ImportError:
            err_console.print("[error]✗[/error] httpx is required for --url mode. Install pyfly[client].")
            raise SystemExit(1) from None
        url = f"{self._base}/actuator/{endpoint.lstrip('/')}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            err_console.print(f"[error]✗[/error] Request to {url} failed: {exc}")
            raise SystemExit(1) from None
