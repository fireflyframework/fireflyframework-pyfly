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
"""Tests for the CLI plugin system and command registration."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from pyfly.cli import plugins


class _FakeEP:
    def __init__(self, name, obj):
        self.name = name
        self.value = f"fake.module:{name}"
        self.dist = type("D", (), {"name": "fake-dist"})()
        self._obj = obj

    def load(self):
        return self._obj


@click.command("hello-plugin")
def _hello() -> None:
    click.echo("hi from plugin")


def test_discover_returns_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "_iter_entry_points", lambda: [_FakeEP("hello-plugin", _hello)])
    discovered = plugins.discover_cli_plugins()
    assert any(name == "hello-plugin" for name, _cmd in discovered)


def test_plugins_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "_iter_entry_points", lambda: [_FakeEP("hello-plugin", _hello)])
    result = CliRunner().invoke(plugins.plugins_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "hello-plugin" in result.output


def test_plugins_list_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "_iter_entry_points", lambda: [])
    result = CliRunner().invoke(plugins.plugins_group, ["list"])
    assert result.exit_code == 0, result.output


def test_new_commands_in_root_help() -> None:
    from pyfly.cli.main import cli

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in (
        "test",
        "lint",
        "format",
        "typecheck",
        "features",
        "add",
        "remove",
        "build",
        "completion",
        "upgrade",
        "plugins",
    ):
        assert name in result.output, f"{name} missing from root help"
