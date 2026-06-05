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
"""Regression: the generated hexagonal archetype actually WIRES its ports (v26.06.16).

The archetype used to define inbound/outbound ports that nothing inherited, so they
were dead code — ``resolve(TodoRepositoryPort)`` raised ``NoSuchBeanError`` and the
service docstring "Implements the inbound ports" was false. The application service
now implements the inbound use-case ports and the in-memory adapter implements the
outbound repository port (so both resolve via the DI scanner's MRO binding).
"""

from __future__ import annotations

import ast
from pathlib import Path

from pyfly.cli.templates import generate_project


def _src(project_dir: Path, package: str, rel: str) -> str:
    text = (project_dir / "src" / package / rel).read_text(encoding="utf-8")
    ast.parse(text)  # generated file must be valid Python
    return text


class TestHexagonalArchetypeWiring:
    def test_ports_are_wired(self, tmp_path: Path) -> None:
        proj = tmp_path / "hex"
        generate_project(name="todo", project_dir=proj, archetype="hexagonal", features=["web"], package_name="todo")

        services = _src(proj, "todo", "application/services.py")
        # The service implements ALL four inbound use-case ports.
        assert "class TodoService(CreateTodoUseCase, GetTodoUseCase, ListTodosUseCase, DeleteTodoUseCase):" in services
        assert "from todo.domain.ports.inbound import" in services
        assert "async def create" in services  # async use-case boundary

        # The in-memory adapter implements the outbound repository port.
        persistence = _src(proj, "todo", "infrastructure/adapters/persistence.py")
        assert "class InMemoryTodoRepository(TodoRepositoryPort):" in persistence
        assert "from todo.domain.ports.outbound import TodoRepositoryPort" in persistence

        # The response DTO uses the domain id type (str), not the entity int.
        dto = _src(proj, "todo", "api/dto.py")
        assert "id: str" in dto
        assert "id: int" not in dto
