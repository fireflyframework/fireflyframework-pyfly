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
"""Regression: scaffolded ASGI entry points are ``mypy --strict``-friendly.

The generated ``main.py`` lifespan was untyped (``async def _lifespan(app):``),
so a scaffolded project that enables ``mypy --strict`` (which PyFly's conventions
mandate) failed type-checking on code it never wrote. The lifespan is now fully
annotated in both the ``web-api`` and SSR ``web`` entry-point templates.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pyfly.cli.templates import generate_project

_TYPED_LIFESPAN = "async def _lifespan(app: Starlette) -> AsyncIterator[None]:"


def _main_src(project_dir: Path, package: str) -> str:
    src = (project_dir / "src" / package / "main.py").read_text(encoding="utf-8")
    ast.parse(src)  # generated entry point must be valid Python
    return src


class TestScaffoldedMainIsTyped:
    def test_web_api_main_lifespan_is_typed(self, tmp_path: Path) -> None:
        generate_project(
            name="shop", project_dir=tmp_path / "api", archetype="web-api", features=["web"], package_name="shop"
        )
        src = _main_src(tmp_path / "api", "shop")
        assert _TYPED_LIFESPAN in src
        assert "from collections.abc import AsyncIterator" in src
        assert "from starlette.applications import Starlette" in src
        assert "async def _lifespan(app):" not in src  # the old untyped form is gone

    def test_web_ssr_main_lifespan_is_typed(self, tmp_path: Path) -> None:
        generate_project(
            name="site", project_dir=tmp_path / "web", archetype="web", features=["web"], package_name="site"
        )
        src = _main_src(tmp_path / "web", "site")
        assert _TYPED_LIFESPAN in src
        assert "async def _lifespan(app):" not in src
