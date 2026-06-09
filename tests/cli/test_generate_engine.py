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
"""Tests for the generate engine and the service generator."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pyfly.cli.generate import Artifact, generate_group, write_artifacts


def _scaffold(root: Path, package: str = "shop", archetype: str = "web-api") -> None:
    (root / "src" / package).mkdir(parents=True)
    (root / "src" / package / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname = "{package}"\n')
    (root / "pyfly.yaml").write_text(f"pyfly:\n  app:\n    name: {package}\n    archetype: {archetype}\n")


class TestEngine:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        target = tmp_path / "a.py"
        actions = write_artifacts([Artifact("x", target, "hello\n")], force=False, dry_run=False)
        assert target.read_text() == "hello\n"
        assert actions == [("create", target)]

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        target = tmp_path / "a.py"
        actions = write_artifacts([Artifact("x", target, "hi\n")], force=False, dry_run=True)
        assert not target.exists()
        assert actions == [("create", target)]

    def test_skip_existing_without_force(self, tmp_path: Path) -> None:
        target = tmp_path / "a.py"
        target.write_text("old\n")
        actions = write_artifacts([Artifact("x", target, "new\n")], force=False, dry_run=False)
        assert target.read_text() == "old\n"
        assert actions == [("skip", target)]

    def test_overwrite_with_force(self, tmp_path: Path) -> None:
        target = tmp_path / "a.py"
        target.write_text("old\n")
        write_artifacts([Artifact("x", target, "new\n")], force=True, dry_run=False)
        assert target.read_text() == "new\n"


class TestServiceGenerator:
    def test_generate_service(self, tmp_path: Path) -> None:
        _scaffold(tmp_path)
        runner = CliRunner()
        result = runner.invoke(generate_group, ["service", "Pricing"], obj={"cwd": tmp_path})
        assert result.exit_code == 0, result.output
        svc = tmp_path / "src" / "shop" / "services" / "pricing_service.py"
        assert svc.exists()
        text = svc.read_text()
        assert "class PricingService" in text
        assert "@service" in text
        assert (tmp_path / "tests" / "test_pricing_service.py").exists()
        assert (tmp_path / "src" / "shop" / "services" / "__init__.py").exists()

    def test_generate_service_dry_run(self, tmp_path: Path) -> None:
        _scaffold(tmp_path)
        runner = CliRunner()
        result = runner.invoke(generate_group, ["service", "Pricing", "--dry-run"], obj={"cwd": tmp_path})
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "src" / "shop" / "services" / "pricing_service.py").exists()
