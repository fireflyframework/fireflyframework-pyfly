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
"""Tests for individual generate subcommands."""

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


def run(args: list[str], cwd: Path):
    return CliRunner().invoke(generate_group, args, obj={"cwd": cwd})


class TestControllerGenerator:
    def test_rest_controller(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="web-api")
        result = run(["controller", "Order"], tmp_path)
        assert result.exit_code == 0, result.output
        f = tmp_path / "src" / "shop" / "controllers" / "order_controller.py"
        text = f.read_text()
        assert "@rest_controller" in text
        assert '@request_mapping("/orders")' in text
        assert "class OrderController" in text
        assert (tmp_path / "tests" / "test_order_controller.py").exists()

    def test_web_controller_for_web_archetype(self, tmp_path: Path) -> None:
        scaffold(tmp_path, archetype="web")
        result = run(["controller", "Page"], tmp_path)
        assert result.exit_code == 0, result.output
        text = (tmp_path / "src" / "shop" / "controllers" / "page_controller.py").read_text()
        assert "@controller" in text
        assert "Jinja2Templates" in text
