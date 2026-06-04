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
"""CQRS base types — Command and Query.

``Command`` and ``Query`` are **not** dataclasses so that subclasses can
freely use ``@dataclass(frozen=True)`` or any other pattern.  Metadata
(IDs, timestamps, correlation) is exposed via methods rather than fields,
mirroring Java's ``default`` interface methods.

Handlers live in :mod:`pyfly.cqrs.command.handler` (``CommandHandler[C, R]``)
and :mod:`pyfly.cqrs.query.handler` (``QueryHandler[Q, R]``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar, cast
from uuid import uuid4

from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs.validation.types import ValidationResult

R = TypeVar("R")


class Command(Generic[R]):
    """Base class for commands (write operations).

    Subclass as a ``@dataclass`` (frozen or not) and add domain-specific fields::

        @dataclass(frozen=True)
        class CreateOrderCommand(Command[OrderId]):
            customer_id: str
            items: list[OrderItem]

    Metadata (command_id, correlation_id, etc.) is accessed and set via
    methods.  This avoids conflicts with frozen dataclass subclasses.
    """

    # ── metadata accessors ─────────────────────────────────────

    def get_command_id(self) -> str:
        """Unique identifier for this command instance (auto-generated UUID)."""
        try:
            return cast(str, self._cqrs_command_id)  # type: ignore[attr-defined]
        except AttributeError:
            cid = str(uuid4())
            object.__setattr__(self, "_cqrs_command_id", cid)
            return cid

    def get_correlation_id(self) -> str | None:
        """Correlation ID for distributed tracing."""
        return getattr(self, "_cqrs_correlation_id", None)

    def set_correlation_id(self, correlation_id: str) -> None:
        object.__setattr__(self, "_cqrs_correlation_id", correlation_id)

    def get_timestamp(self) -> datetime:
        """When this command was created."""
        try:
            return cast(datetime, self._cqrs_timestamp)  # type: ignore[attr-defined]
        except AttributeError:
            ts = datetime.now(UTC)
            object.__setattr__(self, "_cqrs_timestamp", ts)
            return ts

    def get_initiated_by(self) -> str | None:
        """User or system that initiated this command."""
        return getattr(self, "_cqrs_initiated_by", None)

    def set_initiated_by(self, user_id: str) -> None:
        object.__setattr__(self, "_cqrs_initiated_by", user_id)

    def get_metadata(self) -> dict[str, Any]:
        """Arbitrary metadata key-value pairs."""
        try:
            return cast(dict[str, Any], self._cqrs_metadata)  # type: ignore[attr-defined]
        except AttributeError:
            md: dict[str, Any] = {}
            object.__setattr__(self, "_cqrs_metadata", md)
            return md

    def set_metadata(self, key: str, value: Any) -> None:
        self.get_metadata()[key] = value

    # ── hooks for bus pipeline ─────────────────────────────────

    def get_cache_key(self) -> str | None:
        """Override to provide a cache-invalidation key."""
        return None

    async def validate(self) -> ValidationResult:
        """Custom business-rule validation.  Override in subclass."""
        return ValidationResult.success()

    async def authorize(self) -> AuthorizationResult:
        """Authorize without execution context.  Override in subclass."""
        return AuthorizationResult.success()

    async def authorize_with_context(self, ctx: Any) -> AuthorizationResult:
        """Authorize with an :class:`ExecutionContext`.  Override in subclass."""
        return await self.authorize()


class Query(Generic[R]):
    """Base class for queries (read operations).

    Subclass as a ``@dataclass`` (frozen or not) and add domain-specific fields::

        @dataclass(frozen=True)
        class GetOrderQuery(Query[Order | None]):
            order_id: str
    """

    # ── metadata accessors ─────────────────────────────────────

    def get_query_id(self) -> str:
        try:
            return cast(str, self._cqrs_query_id)  # type: ignore[attr-defined]
        except AttributeError:
            qid = str(uuid4())
            object.__setattr__(self, "_cqrs_query_id", qid)
            return qid

    def get_correlation_id(self) -> str | None:
        return getattr(self, "_cqrs_correlation_id", None)

    def set_correlation_id(self, correlation_id: str) -> None:
        object.__setattr__(self, "_cqrs_correlation_id", correlation_id)

    def get_timestamp(self) -> datetime:
        try:
            return cast(datetime, self._cqrs_timestamp)  # type: ignore[attr-defined]
        except AttributeError:
            ts = datetime.now(UTC)
            object.__setattr__(self, "_cqrs_timestamp", ts)
            return ts

    def get_metadata(self) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], self._cqrs_metadata)  # type: ignore[attr-defined]
        except AttributeError:
            md: dict[str, Any] = {}
            object.__setattr__(self, "_cqrs_metadata", md)
            return md

    def is_cacheable(self) -> bool:
        """Whether this query result can be cached.  Default ``True``."""
        return getattr(self, "_cqrs_cacheable", True)

    def set_cacheable(self, enabled: bool) -> None:
        object.__setattr__(self, "_cqrs_cacheable", enabled)

    def get_cache_key(self) -> str | None:
        """Smart cache key — override for custom keys, else auto-generated from class + fields.

        Uses a stable SHA-256 digest (not the process-randomized built-in
        ``hash()``) so the same query maps to the same key across processes and
        restarts (audit #100).
        """
        import dataclasses
        import hashlib

        if not dataclasses.is_dataclass(self):
            return type(self).__name__
        fields = {f.name: repr(getattr(self, f.name)) for f in dataclasses.fields(self)}
        digest = hashlib.sha256(repr(sorted(fields.items())).encode("utf-8")).hexdigest()[:16]
        return f"{type(self).__name__}:{digest}"

    # ── hooks for bus pipeline ─────────────────────────────────

    async def validate(self) -> ValidationResult:
        """Custom business-rule validation.  Override in subclass."""
        return ValidationResult.success()

    async def authorize(self) -> AuthorizationResult:
        """Authorize without execution context.  Override in subclass."""
        return AuthorizationResult.success()

    async def authorize_with_context(self, ctx: Any) -> AuthorizationResult:
        """Authorize with an :class:`ExecutionContext`.  Override in subclass."""
        return await self.authorize()
