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
"""PyFlyApplication.run() dispatches the shell — the generated CLI archetype
no longer crashes on a missing run() (audit #1)."""

from __future__ import annotations

import pytest

from pyfly.container.types import Scope
from pyfly.core.application import PyFlyApplication, pyfly_application
from pyfly.shell.ports.outbound import ShellRunnerPort


class _StubRunner:
    def __init__(self) -> None:
        self.ran_with: list[str] | None = None
        self.interactive = False

    def register_command(self, *args: object, **kwargs: object) -> None: ...

    async def run(self, args: list[str] | None = None) -> int:
        self.ran_with = list(args or [])
        return 0

    async def run_interactive(self) -> None:
        self.interactive = True


def test_pyfly_application_has_run_coroutine() -> None:
    assert hasattr(PyFlyApplication, "run")  # the archetype's main() awaits this


@pytest.mark.asyncio
async def test_run_dispatches_args_to_shell_runner() -> None:
    @pyfly_application(name="testcli")
    class App:
        pass

    pyfly = PyFlyApplication(App)
    runner = _StubRunner()
    # Pre-register a stub runner so startup's conditional shell auto-config is
    # skipped and run() resolves this instance.
    pyfly.context._container.register(ShellRunnerPort, scope=Scope.SINGLETON)  # noqa: SLF001
    pyfly.context._container._registrations[ShellRunnerPort].instance = runner  # noqa: SLF001

    rc = await pyfly.run(["hello", "--name", "x"])
    assert rc == 0
    assert runner.ran_with == ["hello", "--name", "x"]


@pytest.mark.asyncio
async def test_run_without_args_starts_interactive() -> None:
    @pyfly_application(name="testcli")
    class App:
        pass

    pyfly = PyFlyApplication(App)
    runner = _StubRunner()
    pyfly.context._container.register(ShellRunnerPort, scope=Scope.SINGLETON)  # noqa: SLF001
    pyfly.context._container._registrations[ShellRunnerPort].instance = runner  # noqa: SLF001

    await pyfly.run([])
    assert runner.interactive is True
