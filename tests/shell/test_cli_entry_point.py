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
"""The ``cli`` archetype's entry point exits with the command's exit code (WP13-09, C077).

The generated ``main()`` ran ``asyncio.run(pyfly.run())`` and dropped the result, so a failing command
exited 0 and a cron job saw success. It now raises ``SystemExit`` with the exit code ``PyFlyApplication.run()``
returns, having run the command on the application's loop.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from pyfly.cli.templates import generate_project


class _Application:
    """Stands in for ``PyFlyApplication``: returns a chosen exit code from ``run()``."""

    exit_code = 0
    ran = False

    def __init__(self, app_class: type) -> None:
        self.app_class = app_class

    async def run(self, args: list[str] | None = None) -> int:
        type(self).ran = True
        return type(self).exit_code


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_the_generated_main_exits_with_the_commands_exit_code(
    exit_code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate_project("wp13-tool", tmp_path, "cli", ["shell"], package_name="wp13_tool")
    source = (tmp_path / "src" / "wp13_tool" / "main.py").read_text()

    app_module = types.ModuleType("wp13_tool.app")
    app_module.Application = type("Application", (), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wp13_tool", types.ModuleType("wp13_tool"))
    monkeypatch.setitem(sys.modules, "wp13_tool.app", app_module)
    monkeypatch.setattr("pyfly.core.PyFlyApplication", _Application)
    _Application.exit_code = exit_code
    namespace: dict[str, Any] = {"__name__": "wp13_tool.main"}
    exec(compile(source, "main.py", "exec"), namespace)  # noqa: S102 — the generated entry point under test

    with pytest.raises(SystemExit) as exited:
        namespace["main"]()
    assert exited.value.code == exit_code
    assert _Application.ran
