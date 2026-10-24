"""Startup runners must accept CLI arguments, not merely expose run()."""

from pyfly.context import ApplicationContext
from pyfly.shell.runner import ApplicationArguments, CommandLineRunner


def test_run_service_is_not_a_startup_runner():
    class Service:
        async def run(self, prompt: str):
            raise AssertionError("service invoked during startup")

    assert not ApplicationContext._is_runner(Service())


def test_structural_and_explicit_startup_runner_signatures_remain_supported():
    class Cli:
        async def run(self, args: list[str]) -> None:
            pass

    class App:
        async def run(self, args: ApplicationArguments) -> None:
            pass

    class Legacy:
        async def run(self, args):
            pass

    class Explicit(CommandLineRunner):
        async def run(self, values: list[str]) -> None:
            pass

    for runner in (Cli(), App(), Legacy(), Explicit()):
        assert ApplicationContext._is_runner(runner)
