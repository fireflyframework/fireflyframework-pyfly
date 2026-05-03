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
"""Entity auditing — populates audit fields via SQLAlchemy ORM events.

Registers ``before_insert`` and ``before_update`` listeners on
:class:`~pyfly.data.relational.sqlalchemy.entity.BaseEntity` so that
``created_at``, ``updated_at``, ``created_by``, and ``updated_by`` are
set automatically. The current user is resolved from
:class:`~pyfly.context.request_context.RequestContext`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class AuditingEntityListener:
    """Registers SQLAlchemy ORM events to populate audit fields on BaseEntity.

    Call :meth:`register` once at startup to attach ``before_insert`` and
    ``before_update`` listeners that set timestamp and user-identity columns.
    """

    def register(self) -> None:
        """Attach ORM event listeners for insert and update auditing."""
        from sqlalchemy import event

        from pyfly.data.relational.sqlalchemy.entity import BaseEntity

        event.listen(BaseEntity, "before_insert", self._on_insert, propagate=True)
        event.listen(BaseEntity, "before_update", self._on_update, propagate=True)
        logger.info("Registered entity auditing listeners on BaseEntity")

    def _on_insert(self, mapper: Any, connection: Any, target: Any) -> None:
        """Set all audit fields on a new entity."""
        now = datetime.now(UTC)
        target.created_at = now
        target.updated_at = now
        user = self._get_current_user()
        if user is not None:
            target.created_by = user
            target.updated_by = user

    def _on_update(self, mapper: Any, connection: Any, target: Any) -> None:
        """Update modification timestamp and user on an existing entity."""
        target.updated_at = datetime.now(UTC)
        user = self._get_current_user()
        if user is not None:
            target.updated_by = user

    def _get_current_user(self) -> str | None:
        """Resolve the current authenticated user from RequestContext."""
        from pyfly.context.request_context import RequestContext

        ctx = RequestContext.current()
        if ctx is None:
            return None
        sc = ctx.security_context
        if sc is None or not sc.is_authenticated:
            return None
        return sc.user_id
