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
"""Auto-configuration discovery guardrail (v26.06.60).

Every ``@auto_configuration`` class must be reachable via a ``pyfly.auto_configuration``
entry point — otherwise it is silently never wired at startup (the v26.06.55 session-
concurrency bug). This test AST-scans the source so a forgotten entry point fails CI.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from pyfly.config.auto import discover_auto_configurations
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.session.concurrency import SessionConcurrencyController

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "pyfly"


def _declared_auto_configuration_classes() -> set[str]:
    """Names of all classes decorated with ``@auto_configuration`` in the source tree."""
    names: set[str] = set()
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Name) and target.id == "auto_configuration":
                    names.add(node.name)
    return names


def test_every_auto_configuration_class_has_an_entry_point() -> None:
    declared = _declared_auto_configuration_classes()
    discovered = {c.__name__ for c in discover_auto_configurations()}
    missing = declared - discovered
    assert not missing, (
        f"@auto_configuration classes with no 'pyfly.auto_configuration' entry point "
        f"(they will never be wired at startup): {sorted(missing)}"
    )


@pytest.mark.asyncio
async def test_session_concurrency_controller_is_autowired_when_enabled() -> None:
    cfg = Config(
        {"pyfly": {"session": {"enabled": "true", "concurrency": {"enabled": "true", "max-sessions": 2}}}}
    )
    ctx = ApplicationContext(cfg)
    await ctx.start()
    assert isinstance(ctx.get_bean(SessionConcurrencyController), SessionConcurrencyController)
