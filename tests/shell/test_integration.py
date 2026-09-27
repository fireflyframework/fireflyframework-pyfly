# tests/shell/test_integration.py
"""End-to-end integration test for the shell subsystem.

Simulates a CLI application with DI, @shell_component commands, and CommandLineRunner.
"""

import pytest

from pyfly.container.stereotypes import component, service, shell_component
from pyfly.container.types import Scope
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.shell.adapters.click_adapter import ClickShellAdapter
from pyfly.shell.decorators import shell_method
from pyfly.shell.ports.outbound import ShellRunnerPort


@service
class UserService:
    def find(self, name: str) -> dict:
        return {"name": name, "display_name": name.title()}


@shell_component
class GreetingCommands:
    def __init__(self, user_service: UserService) -> None:
        self._user_service = user_service

    @shell_method(key="greet", help="Greet a user by name")
    def greet(self, name: str) -> str:
        user = self._user_service.find(name)
        return f"Hello, {user['display_name']}!"

    @shell_method(key="farewell", help="Say goodbye to a user")
    def farewell(self, name: str, formal: bool = False) -> str:
        user = self._user_service.find(name)
        if formal:
            return f"Farewell, {user['display_name']}. Until we meet again."
        return f"Bye, {user['display_name']}!"


class TestShellIntegration:
    @pytest.mark.asyncio
    async def test_full_cli_flow(self):
        """Boot a CLI app with DI, wire commands, and execute them."""
        runner = ClickShellAdapter()

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserService)
        ctx.register_bean(GreetingCommands)
        ctx._container.register(ShellRunnerPort, scope=Scope.SINGLETON)
        ctx._container._registrations[ShellRunnerPort].instance = runner

        await ctx.start()

        # Verify commands were wired
        assert ctx.wiring_counts["shell_commands"] == 2

        # Execute greet command
        exit_code, output = runner.invoke(["greet", "john"])
        assert exit_code == 0
        assert output == "Hello, John!"

        # Execute farewell command with flag
        exit_code, output = runner.invoke(["farewell", "john", "--formal"])
        assert exit_code == 0
        assert output == "Farewell, John. Until we meet again."

        # Execute farewell command without flag
        exit_code, output = runner.invoke(["farewell", "jane"])
        assert exit_code == 0
        assert output == "Bye, Jane!"

        await ctx.stop()

    @pytest.mark.asyncio
    async def test_command_line_runner_with_shell_commands(self):
        """CommandLineRunner and @shell_component commands coexist."""
        runner_invoked = []

        @component
        class StartupRunner:
            async def run(self, args: list[str]) -> None:
                runner_invoked.append(True)

        shell_runner = ClickShellAdapter()

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserService)
        ctx.register_bean(GreetingCommands)
        ctx.register_bean(StartupRunner)
        ctx._container.register(ShellRunnerPort, scope=Scope.SINGLETON)
        ctx._container._registrations[ShellRunnerPort].instance = shell_runner

        await ctx.start()

        # CommandLineRunner was invoked
        assert len(runner_invoked) == 1

        # Shell commands also work
        exit_code, output = shell_runner.invoke(["greet", "world"])
        assert exit_code == 0
        assert output == "Hello, World!"

        await ctx.stop()

    @pytest.mark.asyncio
    async def test_async_shell_method(self):
        """Async @shell_method commands work correctly."""

        @shell_component
        class AsyncCommands:
            @shell_method(key="fetch", help="Fetch a URL")
            async def fetch(self, url: str) -> str:
                return f"Fetched {url}"

        shell_runner = ClickShellAdapter()
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(AsyncCommands)
        ctx._container.register(ShellRunnerPort, scope=Scope.SINGLETON)
        ctx._container._registrations[ShellRunnerPort].instance = shell_runner

        await ctx.start()

        # An async command runs on the application's loop: ainvoke() awaits it there.
        exit_code, output = await shell_runner.ainvoke(["fetch", "https://example.com"])
        assert exit_code == 0
        assert output == "Fetched https://example.com"

        # The sync invoke() would have to run it on another loop: it refuses instead.
        with pytest.raises(RuntimeError, match="ainvoke"):
            shell_runner.invoke(["fetch", "https://example.com"])

        await ctx.stop()
