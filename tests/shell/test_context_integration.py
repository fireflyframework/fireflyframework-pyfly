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
"""Tests for shell wiring in ApplicationContext."""

import pytest

from pyfly.container.stereotypes import component, shell_component
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.shell.adapters.click_adapter import ClickShellAdapter
from pyfly.shell.decorators import shell_method
from pyfly.shell.ports.outbound import ShellRunnerPort
from pyfly.shell.runner import ApplicationArguments


class TestShellWiring:
    @pytest.mark.asyncio
    async def test_shell_commands_wired_to_runner(self):
        """@shell_component methods with @shell_method are registered with the ShellRunnerPort."""

        @shell_component
        class GreetCommands:
            @shell_method(key="greet", help="Greet someone")
            def greet(self, name: str) -> str:
                return f"Hello, {name}!"

        runner = ClickShellAdapter()
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(GreetCommands)

        # Pre-register the runner as a bean
        from pyfly.container.types import Scope

        ctx._container.register(ShellRunnerPort, scope=Scope.SINGLETON)
        ctx._container._registrations[ShellRunnerPort].instance = runner

        await ctx.start()

        # Verify command was registered
        exit_code, output = runner.invoke(["greet", "World"])
        assert exit_code == 0
        assert "Hello, World!" in output

    @pytest.mark.asyncio
    async def test_wiring_counts_tracked(self):
        """Shell command wiring count is tracked in wiring_counts."""

        @shell_component
        class Commands:
            @shell_method(key="cmd1")
            def cmd1(self) -> str:
                return "1"

            @shell_method(key="cmd2")
            def cmd2(self) -> str:
                return "2"

        runner = ClickShellAdapter()
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(Commands)

        from pyfly.container.types import Scope

        ctx._container.register(ShellRunnerPort, scope=Scope.SINGLETON)
        ctx._container._registrations[ShellRunnerPort].instance = runner

        await ctx.start()
        assert ctx.wiring_counts.get("shell_commands", 0) == 2


class TestCommandLineRunnerInvocation:
    @pytest.mark.asyncio
    async def test_command_line_runner_invoked(self):
        """@component implementing CommandLineRunner is auto-invoked after startup."""
        invocations: list[list[str]] = []

        @component
        class MyRunner:
            async def run(self, args: list[str]) -> None:
                invocations.append(args)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(MyRunner)
        await ctx.start()

        # Runner should have been invoked (with empty args since not passed via CLI)
        assert len(invocations) == 1

    @pytest.mark.asyncio
    async def test_application_runner_invoked(self):
        """@component implementing ApplicationRunner is auto-invoked after startup."""
        invocations: list[ApplicationArguments] = []

        @component
        class MyRunner:
            async def run(self, args: ApplicationArguments) -> None:
                invocations.append(args)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(MyRunner)
        await ctx.start()

        assert len(invocations) == 1
        assert isinstance(invocations[0], ApplicationArguments)
