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
"""Tests for completion and upgrade commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pyfly.cli import completion


class TestCompletion:
    def test_bash_completion_script(self) -> None:
        result = CliRunner().invoke(completion.completion_cmd, ["bash"])
        assert result.exit_code == 0, result.output
        assert "_PYFLY_COMPLETE" in result.output or "complete" in result.output.lower()

    def test_zsh_completion_script(self) -> None:
        result = CliRunner().invoke(completion.completion_cmd, ["zsh"])
        assert result.exit_code == 0, result.output
        assert result.output.strip()


class TestUpgrade:
    def test_upgrade_invokes_installer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        class _Result:
            returncode = 0

        monkeypatch.setattr(completion.shutil, "which", lambda t: "/usr/bin/uv" if t == "uv" else None)
        monkeypatch.setattr(completion.subprocess, "run", lambda cmd, **k: (calls.append(list(cmd)), _Result())[1])
        result = CliRunner().invoke(completion.upgrade_cmd, [])
        assert result.exit_code == 0, result.output
        assert "pyfly" in calls[-1]
