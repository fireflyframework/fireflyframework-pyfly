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
"""Tests for the composite 'resource' generator."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pyfly.cli.generate import generate_group


def scaffold(root: Path, package: str = "shop", archetype: str = "web-api") -> None:
    (root / "src" / package).mkdir(parents=True)
    (root / "src" / package / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname = "{package}"\n')
    (root / "pyfly.yaml").write_text(
        f"pyfly:\n  app:\n    name: {package}\n    archetype: {archetype}\n"
    )


class TestResource:
    def test_resource_generates_full_stack(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = CliRunner().invoke(generate_group, ["resource", "Product"], obj={"cwd": tmp_path})
        assert result.exit_code == 0, result.output
        base = tmp_path / "src" / "shop"
        assert (base / "models" / "product.py").exists()
        assert (base / "dto" / "product_dto.py").exists()
        assert (base / "repositories" / "product_repository.py").exists()
        assert (base / "services" / "product_service.py").exists()
        assert (base / "controllers" / "product_controller.py").exists()
        assert (tmp_path / "tests" / "test_product_service.py").exists()

    def test_resource_dry_run(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        result = CliRunner().invoke(
            generate_group, ["resource", "Product", "--dry-run"], obj={"cwd": tmp_path}
        )
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "src" / "shop" / "models" / "product.py").exists()
