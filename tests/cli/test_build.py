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
"""Tests for build/packaging commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pyfly.cli import build


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch):
    calls = []

    class _Result:
        returncode = 0
        stdout = "abc123\n"

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(build.subprocess, "run", _run)
    monkeypatch.setattr(build.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    return calls


class TestBuild:
    def test_wheel_uses_uv_build(self, fake_run) -> None:
        result = CliRunner().invoke(build.build_group, ["wheel"])
        assert result.exit_code == 0, result.output
        assert fake_run[-1][:2] == ["uv", "build"] and "--wheel" in fake_run[-1]

    def test_sdist_uses_uv_build(self, fake_run) -> None:
        result = CliRunner().invoke(build.build_group, ["sdist"])
        assert result.exit_code == 0, result.output
        assert "--sdist" in fake_run[-1]

    def test_info_writes_build_info(self, fake_run, tmp_path: Path) -> None:
        result = CliRunner().invoke(build.build_group, ["info", "-o", str(tmp_path / "build-info.json")])
        assert result.exit_code == 0, result.output
        data = json.loads((tmp_path / "build-info.json").read_text())
        assert "git" in data and "build" in data

    def test_image_pack(self, fake_run) -> None:
        result = CliRunner().invoke(build.build_group, ["image", "--tag", "app:1", "--builder", "pack"])
        assert result.exit_code == 0, result.output
        assert fake_run[-1][0] == "pack"

    def test_image_docker(self, fake_run) -> None:
        result = CliRunner().invoke(build.build_group, ["image", "--tag", "app:1", "--builder", "docker"])
        assert result.exit_code == 0, result.output
        assert fake_run[-1][0] == "docker"
