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
"""Regression: the scaffolded todo_service guards a missing id (v26.06.20).

The generated service dereferenced ``find_by_id`` without a None check — a latent
AttributeError on a missing id and a ``mypy --strict`` union-attr error in the
data-relational/data-document variants. It now raises ResourceNotFoundException
(-> 404) across all variants, and the in-memory repository returns Optional.
"""

from __future__ import annotations

from pathlib import Path

from pyfly.cli.templates import generate_project


def _service_src(project_dir: Path, package: str) -> str:
    return (project_dir / "src" / package / "services" / "todo_service.py").read_text(encoding="utf-8")


def test_in_memory_service_guards_missing(tmp_path: Path) -> None:
    proj = tmp_path / "inmem"
    generate_project(name="svc", project_dir=proj, archetype="web-api", features=["web"], package_name="svc")
    src = _service_src(proj, "svc")
    assert src.count("TODO_NOT_FOUND") >= 2  # get + toggle_complete guarded
    assert "if todo is None:" in src
    repo = (proj / "src" / "svc" / "repositories" / "todo_repository.py").read_text(encoding="utf-8")
    assert "TodoResponse | None" in repo  # in-memory find_by_id returns Optional


def test_data_relational_service_guards_missing(tmp_path: Path) -> None:
    proj = tmp_path / "rel"
    generate_project(
        name="svc", project_dir=proj, archetype="web-api", features=["web", "data-relational"], package_name="svc"
    )
    src = _service_src(proj, "svc")
    assert src.count("TODO_NOT_FOUND") >= 2  # get + toggle_complete guarded
    assert "if entity is None:" in src
