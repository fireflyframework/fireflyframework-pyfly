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
"""Tests for 'pyfly new' flags: --list, --git, --no-input."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from pyfly.cli.main import cli


class TestNewList:
    def test_list_prints_archetypes_and_exits(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["new", "--list"])
        assert result.exit_code == 0, result.output
        assert "web-api" in result.output
        assert "fastapi-api" in result.output
        assert "data-relational" in result.output
        assert not any(tmp_path.iterdir())


class TestNewNoInput:
    def test_no_input_without_name_errors(self) -> None:
        result = CliRunner().invoke(cli, ["new", "--no-input"])
        assert result.exit_code != 0
        assert "name" in result.output.lower()

    def test_no_input_with_name_creates(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["new", "svc", "--no-input", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "svc" / "pyproject.toml").exists()


class TestNewGit:
    def test_git_initializes_repo(self, tmp_path: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not available")
        result = CliRunner().invoke(cli, ["new", "svc", "--git", "--directory", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "svc" / ".git").is_dir()
        log = subprocess.run(
            ["git", "-C", str(tmp_path / "svc"), "log", "--oneline"],
            capture_output=True,
            text=True,
        )
        assert log.returncode == 0 and log.stdout.strip()
