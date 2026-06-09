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
"""Tests for 'pyfly shell'."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pyfly.cli.shell import _build_namespace, shell_cmd


@pytest.fixture
def fixture_app() -> type:
    from pyfly.core.application import pyfly_application

    @pyfly_application(name="shell-app")
    class App:
        pass

    return App


def _boot(monkeypatch, app_cls) -> None:
    from pyfly.cli import _introspect, shell

    monkeypatch.setattr(shell, "boot_context", lambda: _introspect.boot_context(app_class=app_cls))


def test_namespace_has_ctx_and_helpers(fixture_app) -> None:
    from pyfly.cli._introspect import boot_context

    ctx = boot_context(app_class=fixture_app)
    ns = _build_namespace(ctx)
    assert "ctx" in ns
    assert "container" in ns
    assert callable(ns["bean"])


def test_shell_c_evaluates_expression(monkeypatch, fixture_app) -> None:
    _boot(monkeypatch, fixture_app)
    result = CliRunner().invoke(shell_cmd, ["-c", "print(1 + 1)"])
    assert result.exit_code == 0, result.output
    assert "2" in result.output


def test_shell_c_can_use_ctx(monkeypatch, fixture_app) -> None:
    _boot(monkeypatch, fixture_app)
    result = CliRunner().invoke(shell_cmd, ["-c", "print(type(ctx).__name__)"])
    assert result.exit_code == 0, result.output
    assert "Context" in result.output  # ApplicationContext class name contains "Context"
