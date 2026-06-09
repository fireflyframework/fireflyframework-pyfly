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
"""Tests for routes/beans/conditions/env/health introspection commands (offline)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pyfly.cli.introspect_cmds import beans_cmd, conditions_cmd, routes_cmd


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

    @pyfly_application(name="introspect-app")
    class App:
        pass

    # Attach the controller class so the boot helper can register it explicitly.
    App._test_controllers = (WidgetController,)  # type: ignore[attr-defined]
    return App


def _boot(monkeypatch, app_cls) -> None:
    """Patch boot_context so it boots the fixture app and registers any controllers."""
    from pyfly.cli import _introspect, introspect_cmds

    def _fake_boot() -> object:
        ctx = _introspect.boot_context(app_class=app_cls)
        for ctrl in getattr(app_cls, "_test_controllers", ()):
            from pyfly.container.types import Scope

            if ctrl not in ctx.container._registrations:
                ctx.container.register(ctrl, scope=Scope.SINGLETON)
        return ctx

    monkeypatch.setattr(introspect_cmds, "boot_context", _fake_boot)


class TestRoutes:
    def test_routes_offline(self, monkeypatch, fixture_app) -> None:
        _boot(monkeypatch, fixture_app)
        result = CliRunner().invoke(routes_cmd, [])
        assert result.exit_code == 0, result.output
        assert "/widgets" in result.output

    def test_routes_json(self, monkeypatch, fixture_app) -> None:
        _boot(monkeypatch, fixture_app)
        result = CliRunner().invoke(routes_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        assert "{" in result.output


class TestBeans:
    def test_beans_offline_lists_controller(self, monkeypatch, fixture_app) -> None:
        _boot(monkeypatch, fixture_app)
        result = CliRunner().invoke(beans_cmd, [])
        assert result.exit_code == 0, result.output
        assert "WidgetController" in result.output


class TestConditions:
    def test_conditions_offline_runs(self, monkeypatch, fixture_app) -> None:
        _boot(monkeypatch, fixture_app)
        result = CliRunner().invoke(conditions_cmd, [])
        assert result.exit_code == 0, result.output
