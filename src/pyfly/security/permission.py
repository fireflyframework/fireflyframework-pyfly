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
"""PermissionEvaluator — ACL-style ``hasPermission`` SPI (Spring parity).

Install one via :func:`pyfly.security.expression.set_permission_evaluator` to back
``hasPermission(target, 'perm')`` / ``hasPermission(id, 'Type', 'perm')`` method
-security expressions with domain-object permission checks.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PermissionEvaluator(Protocol):
    """Decides whether the current principal holds *permission* on a target object."""

    def has_permission(
        self,
        context: Any,
        target: Any,
        permission: str,
        *,
        target_type: str | None = None,
    ) -> bool:
        """Return whether the principal (in *context*) has *permission* on *target*.

        *target* is the domain object (2-arg form) or its identifier (3-arg form,
        where *target_type* names the object type). *context* is the active
        :class:`~pyfly.security.context.SecurityContext`.
        """
        ...
