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
"""Tests for feature management commands."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pyfly.cli.features import _update_pyfly_extras, add_cmd, features_cmd, remove_cmd


class TestExtraEditing:
    def test_add_to_bare_pyfly(self) -> None:
        assert _update_pyfly_extras('"pyfly"', add=["cache"]) == '"pyfly[cache]"'

    def test_add_to_existing_extras(self) -> None:
        assert _update_pyfly_extras('"pyfly[web]"', add=["cache"]) == '"pyfly[cache,web]"'

    def test_add_dedupes(self) -> None:
        assert _update_pyfly_extras('"pyfly[web]"', add=["web"]) == '"pyfly[web]"'

    def test_remove(self) -> None:
        assert _update_pyfly_extras('"pyfly[cache,web]"', remove=["cache"]) == '"pyfly[web]"'

    def test_remove_last_extra_returns_bare(self) -> None:
        assert _update_pyfly_extras('"pyfly[web]"', remove=["web"]) == '"pyfly"'


def _project(tmp_path: Path, extras: str = "web") -> Path:
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = [\n    "pyfly[' + extras + ']",\n]\n'
    )
    (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: app\n    archetype: web-api\n")
    return tmp_path


def run(cmd, args, cwd):
    return CliRunner().invoke(cmd, args, obj={"cwd": cwd})


class TestFeaturesCommands:
    def test_features_lists(self, tmp_path: Path) -> None:
        _project(tmp_path)
        result = run(features_cmd, [], tmp_path)
        assert result.exit_code == 0, result.output
        assert "cache" in result.output and "web" in result.output

    def test_add_patches_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path)
        result = run(add_cmd, ["cache"], tmp_path)
        assert result.exit_code == 0, result.output
        assert "pyfly[cache,web]" in (tmp_path / "pyproject.toml").read_text()

    def test_add_rejects_unknown(self, tmp_path: Path) -> None:
        _project(tmp_path)
        result = run(add_cmd, ["nope"], tmp_path)
        assert result.exit_code != 0
        assert "nope" in (result.output + (result.stderr or ""))

    def test_remove_patches_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, extras="cache,web")
        result = run(remove_cmd, ["cache", "--yes"], tmp_path)
        assert result.exit_code == 0, result.output
        assert "pyfly[web]" in (tmp_path / "pyproject.toml").read_text()


class TestExtraEditingVersionPin:
    def test_preserves_version_specifier(self) -> None:
        assert _update_pyfly_extras('"pyfly[web]>=26.0"', add=["cache"]) == '"pyfly[cache,web]>=26.0"'

    def test_add_to_versioned_bare(self) -> None:
        assert _update_pyfly_extras('"pyfly>=26.0"', add=["cache"]) == '"pyfly[cache]>=26.0"'

    def test_does_not_match_other_package(self) -> None:
        # A different package that merely starts with 'pyfly' must be left alone.
        assert _update_pyfly_extras('"pyfly-extensions"', add=["cache"]) == '"pyfly-extensions"'


class TestNoPyflyDep:
    def test_add_errors_when_no_pyfly_dep(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')
        result = run(add_cmd, ["cache"], tmp_path)
        assert result.exit_code != 0
