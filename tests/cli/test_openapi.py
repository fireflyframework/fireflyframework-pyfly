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
"""Tests for 'pyfly openapi'."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pyfly.cli.openapi import openapi_cmd


@pytest.fixture
def fixture_app() -> type:
    from pyfly.container import rest_controller
    from pyfly.core.application import pyfly_application
    from pyfly.web import get_mapping, request_mapping

    @rest_controller
    @request_mapping("/widgets")
    class WidgetController:
        @get_mapping("/")
        async def list_widgets(self) -> list[dict]:
            return []

    @pyfly_application(name="openapi-app")
    class App:
        _test_controllers = [WidgetController]

    return App


def _boot(monkeypatch, app_cls) -> None:
    """Patch boot_context so it boots the fixture app and registers any controllers."""
    from pyfly.cli import _introspect
    from pyfly.cli import openapi as openapi_mod

    def _fake_boot() -> object:
        ctx = _introspect.boot_context(app_class=app_cls)
        for ctrl in getattr(app_cls, "_test_controllers", ()):
            from pyfly.container.types import Scope

            if ctrl not in ctx.container._registrations:
                ctx.container.register(ctrl, scope=Scope.SINGLETON)
        return ctx

    monkeypatch.setattr(openapi_mod, "boot_context", _fake_boot)


def test_openapi_json_to_stdout(monkeypatch, fixture_app) -> None:
    _boot(monkeypatch, fixture_app)
    result = CliRunner().invoke(openapi_cmd, [])
    assert result.exit_code == 0, result.output
    spec = json.loads(result.output)
    assert spec["openapi"].startswith("3.")
    assert "paths" in spec
    assert "/widgets/" in spec["paths"], f"expected /widgets/ in paths, got: {list(spec['paths'].keys())}"


def test_openapi_to_file(monkeypatch, fixture_app, tmp_path: Path) -> None:
    _boot(monkeypatch, fixture_app)
    out = tmp_path / "spec.json"
    result = CliRunner().invoke(openapi_cmd, ["-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert json.loads(out.read_text())["openapi"].startswith("3.")
