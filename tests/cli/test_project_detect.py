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
"""Tests for current-project detection used by generators."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyfly.cli._project import ProjectNotFoundError, detect_project, feature_flags


def _scaffold(root: Path, package: str, archetype: str = "web-api", *, data: bool = False) -> None:
    (root / "src" / package).mkdir(parents=True)
    (root / "src" / package / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname = "{package}"\n')
    yaml = f"pyfly:\n  app:\n    name: {package}\n    archetype: {archetype}\n"
    if data:
        yaml += "  data:\n    relational:\n      enabled: true\n"
    (root / "pyfly.yaml").write_text(yaml)


class TestDetectProject:
    def test_detects_package_and_archetype(self, tmp_path: Path) -> None:
        _scaffold(tmp_path, "shop", archetype="fastapi-api")
        info = detect_project(tmp_path)
        assert info.package == "shop"
        assert info.archetype == "fastapi-api"
        assert info.package_dir == tmp_path / "src" / "shop"
        assert info.tests_dir == tmp_path / "tests"

    def test_walks_up_to_find_root(self, tmp_path: Path) -> None:
        _scaffold(tmp_path, "shop")
        nested = tmp_path / "src" / "shop"
        info = detect_project(nested)
        assert info.root == tmp_path

    def test_raises_when_no_project(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectNotFoundError):
            detect_project(tmp_path)

    def test_feature_flags_reads_yaml(self, tmp_path: Path) -> None:
        _scaffold(tmp_path, "shop", data=True)
        info = detect_project(tmp_path)
        flags = feature_flags(info)
        assert flags["has_data"] is True
        assert flags["has_mongodb"] is False
