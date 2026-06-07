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
"""Run-on-startup Alembic migrations (v26.06.61)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pyfly.data.relational.migrations import MigrationRunner


@pytest.mark.asyncio
async def test_start_is_noop_when_alembic_ini_missing(tmp_path: Path) -> None:
    runner = MigrationRunner(config_path=str(tmp_path / "missing.ini"))
    await runner.start()  # must not raise — logs a warning and skips


@pytest.mark.asyncio
async def test_start_runs_upgrade_when_ini_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = migrations\n")
    calls: list[tuple[str, str]] = []

    import alembic.command

    def _fake_upgrade(cfg: Any, revision: str) -> None:
        calls.append((cfg.get_main_option("sqlalchemy.url"), revision))

    monkeypatch.setattr(alembic.command, "upgrade", _fake_upgrade)

    runner = MigrationRunner(url="sqlite+aiosqlite:///app.db", config_path=str(ini), revision="head")
    await runner.start()

    assert calls == [("sqlite+aiosqlite:///app.db", "head")]  # migrates the app's datasource


@pytest.mark.asyncio
async def test_runner_has_lifecycle_methods() -> None:
    runner = MigrationRunner()
    assert callable(runner.start) and callable(runner.stop)
    await runner.stop()  # no-op


def test_migration_auto_configuration_builds_runner() -> None:
    from pyfly.core.config import Config
    from pyfly.data.relational.auto_configuration import MigrationAutoConfiguration

    cfg = Config(
        {"pyfly": {"data": {"relational": {"url": "sqlite+aiosqlite:///app.db", "migrations": {"enabled": "true"}}}}}
    )
    runner = MigrationAutoConfiguration().migration_runner(cfg)
    assert isinstance(runner, MigrationRunner)
