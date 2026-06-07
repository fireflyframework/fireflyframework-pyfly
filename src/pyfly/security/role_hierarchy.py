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
"""Role hierarchy — higher roles imply lower ones (Spring Security ``RoleHierarchy``).

Declare ``ADMIN > USER`` to mean an ``ADMIN`` also has every authority of ``USER``.
``hasRole`` / ``hasAnyRole`` / ``hasAuthority`` in method-security expressions consult the
configured hierarchy, so ``hasRole('USER')`` is satisfied for an ``ADMIN``.
"""

from __future__ import annotations

from collections.abc import Iterable


class RoleHierarchy:
    """Directed role-implication graph with transitive expansion."""

    def __init__(self, edges: dict[str, set[str]] | None = None) -> None:
        # _implies[X] = roles directly implied by X
        self._implies: dict[str, set[str]] = {k: set(v) for k, v in (edges or {}).items()}

    @classmethod
    def from_string(cls, spec: str) -> RoleHierarchy:
        """Parse a hierarchy spec: one ``HIGHER > LOWER`` rule per line (or ``;``-separated).

        Example::

            RoleHierarchy.from_string("ADMIN > MANAGER\\nMANAGER > USER")
        """
        edges: dict[str, set[str]] = {}
        for raw in spec.replace(";", "\n").splitlines():
            line = raw.strip()
            if not line or ">" not in line:
                continue
            higher, lower = (part.strip() for part in line.split(">", 1))
            if higher and lower:
                edges.setdefault(higher, set()).add(lower)
        return cls(edges)

    def expand(self, roles: Iterable[str]) -> set[str]:
        """Return *roles* plus every role transitively implied by them."""
        result: set[str] = set()
        stack = list(roles)
        while stack:
            role = stack.pop()
            if role in result:
                continue
            result.add(role)
            stack.extend(self._implies.get(role, ()))
        return result
