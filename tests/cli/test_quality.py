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
"""Tests for the quality wrapper commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pyfly.cli import quality


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch):
    calls = []

    class _Result:
        returncode = 0

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(quality.subprocess, "run", _run)
    monkeypatch.setattr(quality.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    return calls


class TestQuality:
    def test_test_runs_pytest(self, fake_run) -> None:
        result = CliRunner().invoke(quality.test_cmd, [])
        assert result.exit_code == 0, result.output
        assert fake_run[0][0] == "pytest"

    def test_test_passes_extra_args(self, fake_run) -> None:
        result = CliRunner().invoke(quality.test_cmd, ["-k", "foo"])
        assert result.exit_code == 0, result.output
        assert "-k" in fake_run[0] and "foo" in fake_run[0]

    def test_lint_runs_ruff_check(self, fake_run) -> None:
        result = CliRunner().invoke(quality.lint_cmd, [])
        assert result.exit_code == 0, result.output
        assert fake_run[0][:2] == ["ruff", "check"]

    def test_format_runs_ruff_format(self, fake_run) -> None:
        result = CliRunner().invoke(quality.format_cmd, [])
        assert result.exit_code == 0, result.output
        assert fake_run[0][:2] == ["ruff", "format"]

    def test_format_check_flag(self, fake_run) -> None:
        result = CliRunner().invoke(quality.format_cmd, ["--check"])
        assert result.exit_code == 0, result.output
        assert "--check" in fake_run[0]

    def test_typecheck_runs_mypy(self, fake_run) -> None:
        result = CliRunner().invoke(quality.typecheck_cmd, [])
        assert result.exit_code == 0, result.output
        assert fake_run[0][0] == "mypy"

    def test_missing_tool_errors(self, monkeypatch) -> None:
        monkeypatch.setattr(quality.shutil, "which", lambda tool: None)
        result = CliRunner().invoke(quality.lint_cmd, [])
        assert result.exit_code != 0
        assert "ruff" in result.output.lower()
