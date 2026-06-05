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
"""Regression: scaffolded data-backed projects ship async, runnable service tests.

A web-api scaffolded with ``--features data-relational`` / ``data-document`` generates
an ASYNC, DB-backed ``TodoService``; the generated ``tests/test_todo_service.py`` must
therefore be async (not the sync in-memory pattern), or the project fails ``pytest``
out of the box. A plain (non-data) scaffold keeps the synchronous in-memory tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pyfly.cli.templates import generate_project


def _todo_test_src(project_dir: Path) -> str:
    src = (project_dir / "tests" / "test_todo_service.py").read_text(encoding="utf-8")
    ast.parse(src)  # generated test must be syntactically valid Python
    return src


class TestScaffoldedTodoTests:
    def test_relational_scaffold_emits_async_tests(self, tmp_path: Path) -> None:
        generate_project(
            name="cat", project_dir=tmp_path / "rel", archetype="web-api", features=["web", "data-relational"]
        )
        src = _todo_test_src(tmp_path / "rel")
        assert "async def test_create_todo" in src
        assert "await service.create" in src
        # the broken sync signature (calling async service methods without await) must be gone
        assert "def test_create_todo(self) -> None:" not in src

    def test_document_scaffold_emits_async_tests(self, tmp_path: Path) -> None:
        generate_project(
            name="cat", project_dir=tmp_path / "doc", archetype="web-api", features=["web", "data-document"]
        )
        src = _todo_test_src(tmp_path / "doc")
        assert "async def test_create_todo" in src
        assert "await service." in src

    def test_plain_web_scaffold_stays_sync(self, tmp_path: Path) -> None:
        generate_project(name="cat", project_dir=tmp_path / "web", archetype="web-api", features=["web"])
        src = _todo_test_src(tmp_path / "web")
        assert "def test_create_todo(self) -> None:" in src
        assert "async def test_create_todo" not in src
