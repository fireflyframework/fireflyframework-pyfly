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
"""Tests for CLI: pyfly db commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from pyfly.cli.main import cli


class TestDbInit:
    def test_db_init_creates_alembic_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "init"])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "alembic").is_dir()
        assert (tmp_path / "alembic.ini").is_file()
        assert (tmp_path / "alembic" / "env.py").is_file()

    def test_db_init_env_py_imports_base(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        runner.invoke(cli, ["db", "init"])

        env_py = (tmp_path / "alembic" / "env.py").read_text()
        assert "from pyfly.data.relational.sqlalchemy import Base" in env_py

    def test_db_init_env_py_has_async_support(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        runner.invoke(cli, ["db", "init"])

        env_py = (tmp_path / "alembic" / "env.py").read_text()
        assert "async_engine_from_config" in env_py


class TestDbMigrate:
    def test_db_migrate_requires_init_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "migrate", "-m", "initial"])

        assert result.exit_code != 0
        assert "alembic.ini not found" in result.output


class TestDbUpgrade:
    def test_db_upgrade_requires_init_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "upgrade"])

        assert result.exit_code != 0
        assert "alembic.ini not found" in result.output


class TestDbDowngrade:
    def test_db_downgrade_requires_init_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "downgrade", "base"])

        assert result.exit_code != 0
        assert "alembic.ini not found" in result.output


class TestDbHelp:
    def test_db_help_shows_subcommands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "--help"])

        assert result.exit_code == 0
        assert "init" in result.output
        assert "migrate" in result.output
        assert "upgrade" in result.output
        assert "downgrade" in result.output
