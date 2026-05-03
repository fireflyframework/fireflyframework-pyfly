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
"""Tests for :class:`ClickShellAdapter`."""

from __future__ import annotations

import pytest

from pyfly.shell.adapters.click_adapter import ClickShellAdapter
from pyfly.shell.ports.outbound import ShellRunnerPort
from pyfly.shell.result import ShellParam

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_isinstance_check(self) -> None:
        adapter = ClickShellAdapter()
        assert isinstance(adapter, ShellRunnerPort)


# ---------------------------------------------------------------------------
# Simple command with positional argument
# ---------------------------------------------------------------------------


class TestPositionalArgCommand:
    def test_positional_arg(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        def echo(name: str) -> None:
            captured["name"] = name

        adapter.register_command(
            "echo",
            echo,
            params=[
                ShellParam(name="name", param_type=str, is_option=False),
            ],
        )
        exit_code, output = adapter.invoke(["echo", "hello"])
        assert exit_code == 0
        assert captured["name"] == "hello"


# ---------------------------------------------------------------------------
# Command with --option that has a default
# ---------------------------------------------------------------------------


class TestOptionWithDefault:
    def test_option_default_used(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        def deploy(env: str = "staging") -> None:
            captured["env"] = env

        adapter.register_command(
            "deploy",
            deploy,
            params=[
                ShellParam(
                    name="env",
                    param_type=str,
                    is_option=True,
                    default="staging",
                    help_text="Target environment",
                ),
            ],
        )
        exit_code, _ = adapter.invoke(["deploy"])
        assert exit_code == 0
        assert captured["env"] == "staging"

    def test_option_explicit_value(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        def deploy(env: str = "staging") -> None:
            captured["env"] = env

        adapter.register_command(
            "deploy",
            deploy,
            params=[
                ShellParam(
                    name="env",
                    param_type=str,
                    is_option=True,
                    default="staging",
                ),
            ],
        )
        exit_code, _ = adapter.invoke(["deploy", "--env", "production"])
        assert exit_code == 0
        assert captured["env"] == "production"


# ---------------------------------------------------------------------------
# Command with --flag (boolean)
# ---------------------------------------------------------------------------


class TestFlagParam:
    def test_flag_absent_defaults_false(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        def build(verbose: bool = False) -> None:
            captured["verbose"] = verbose

        adapter.register_command(
            "build",
            build,
            params=[
                ShellParam(
                    name="verbose",
                    param_type=bool,
                    is_option=True,
                    default=False,
                    is_flag=True,
                ),
            ],
        )
        exit_code, _ = adapter.invoke(["build"])
        assert exit_code == 0
        assert captured["verbose"] is False

    def test_flag_present_sets_true(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        def build(verbose: bool = False) -> None:
            captured["verbose"] = verbose

        adapter.register_command(
            "build",
            build,
            params=[
                ShellParam(
                    name="verbose",
                    param_type=bool,
                    is_option=True,
                    default=False,
                    is_flag=True,
                ),
            ],
        )
        exit_code, _ = adapter.invoke(["build", "--verbose"])
        assert exit_code == 0
        assert captured["verbose"] is True


# ---------------------------------------------------------------------------
# Unknown command → non-zero exit code
# ---------------------------------------------------------------------------


class TestCommandNotFound:
    def test_unknown_command_returns_nonzero(self) -> None:
        adapter = ClickShellAdapter()
        exit_code, _ = adapter.invoke(["nonexistent"])
        assert exit_code != 0


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestHelpText:
    def test_help_shows_command_help(self) -> None:
        adapter = ClickShellAdapter(name="myapp", help_text="My cool app")

        def greet() -> None:
            pass

        adapter.register_command("greet", greet, help_text="Say hello")

        # Click prints help to stdout and raises SystemExit(0)
        exit_code, output = adapter.invoke(["greet", "--help"])
        # The help output may be returned or may cause a SystemExit(0).
        # Either way, exit_code should be 0.
        assert exit_code == 0


# ---------------------------------------------------------------------------
# async run() method
# ---------------------------------------------------------------------------


class TestAsyncRun:
    @pytest.mark.asyncio
    async def test_run_returns_exit_code(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        def echo(name: str) -> None:
            captured["name"] = name

        adapter.register_command(
            "echo",
            echo,
            params=[
                ShellParam(name="name", param_type=str, is_option=False),
            ],
        )
        exit_code = await adapter.run(["echo", "hello"])
        assert exit_code == 0
        assert captured["name"] == "hello"


# ---------------------------------------------------------------------------
# Async handler support
# ---------------------------------------------------------------------------


class TestAsyncHandler:
    def test_async_handler_is_invoked(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        async def async_greet(name: str) -> None:
            captured["name"] = name

        adapter.register_command(
            "greet",
            async_greet,
            params=[
                ShellParam(name="name", param_type=str, is_option=False),
            ],
        )
        exit_code, _ = adapter.invoke(["greet", "world"])
        assert exit_code == 0
        assert captured["name"] == "world"


# ---------------------------------------------------------------------------
# Grouped (sub) commands
# ---------------------------------------------------------------------------


class TestGroupedCommands:
    def test_subgroup_command(self) -> None:
        adapter = ClickShellAdapter()
        captured: dict[str, object] = {}

        def migrate() -> None:
            captured["ran"] = True

        adapter.register_command("migrate", migrate, group="db")
        exit_code, _ = adapter.invoke(["db", "migrate"])
        assert exit_code == 0
        assert captured["ran"] is True

    def test_multiple_commands_in_same_group(self) -> None:
        adapter = ClickShellAdapter()
        captured: list[str] = []

        def migrate() -> None:
            captured.append("migrate")

        def seed() -> None:
            captured.append("seed")

        adapter.register_command("migrate", migrate, group="db")
        adapter.register_command("seed", seed, group="db")

        exit_code, _ = adapter.invoke(["db", "migrate"])
        assert exit_code == 0
        assert captured == ["migrate"]

        exit_code, _ = adapter.invoke(["db", "seed"])
        assert exit_code == 0
        assert captured == ["migrate", "seed"]
