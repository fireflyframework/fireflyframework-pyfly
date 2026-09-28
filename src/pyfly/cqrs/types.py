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

import dataclasses
import enum
import hashlib
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar, cast
from uuid import uuid4

from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs.validation.types import ValidationResult

R = TypeVar("R")


def cache_key_digest(*components: object) -> str:
    """The SHA-256 digest of *components*, in full: 64 hex characters.

    Each component is encoded on its own before hashing, as a netstring of its UTF-8 bytes
    (``<length>:<bytes>,``), and ``None`` as ``-``, which no netstring starts with. Two different sequences of
    strings therefore never hash the same bytes: ``("a|b", "c")`` and ``("a", "b|c")`` differ, and so do
    ``None``, ``""`` and ``"None"``. A component that is not a ``str`` (a UUID, a number) counts as its
    ``str()``: ``7`` and ``"7"`` get the same digest. The encoding does not depend on ``repr()``, so the same
    components give the same digest in every process and on every Python version.

    The digest is never truncated. The query cache keys entries by digests of values a client chooses (the
    fields of a query, the ``X-Tenant-Id`` header): against a 64-bit digest a client could search offline for
    a value whose digest equals another caller's and be served that caller's entry, while finding one for the
    full SHA-256 is a second preimage.
    """
    encoded = bytearray()
    for component in components:
        if component is None:
            encoded += b"-"
            continue
        text = component if isinstance(component, str) else str(component)
        data = text.encode("utf-8", "surrogatepass")
        encoded += b"%d:%b," % (len(data), data)
    return hashlib.sha256(encoded).hexdigest()


def canonical_text(value: Any) -> str:
    """*value* as text that does not depend on the process: its ``repr()``, except for containers.

    Sets and frozensets list their elements sorted (``repr()`` lists them in hash order, which changes with
    ``PYTHONHASHSEED`` from one process to the next), dicts list their items sorted by key (two equal dicts
    built in another order have another ``repr()``), and lists, tuples and dataclass instances are rebuilt from
    the canonical text of their parts. Each container is tagged with its type's name.

    Any other value contributes its ``repr()``. That is the same in every process for plain values (``str``,
    numbers, ``None``, ``bool``, enums, dates and times, ``Decimal``, ``UUID``, ``bytes``), but not for an object
    with the default ``repr()`` (``<Filter object at 0x...>``) or one that holds a set (a Pydantic model): such
    a value gets another text in another process.
    """
    if isinstance(value, (set, frozenset)):
        return f"{type(value).__qualname__}({{{', '.join(sorted(canonical_text(item) for item in value))}}})"
    if isinstance(value, dict):
        items = sorted(f"{canonical_text(key)}: {canonical_text(item)}" for key, item in value.items())
        return f"{type(value).__qualname__}({{{', '.join(items)}}})"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__qualname__}([{', '.join(canonical_text(item) for item in value)}])"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = (f"{field.name}={canonical_text(getattr(value, field.name))}" for field in dataclasses.fields(value))
        return f"{type(value).__qualname__}({', '.join(fields)})"
    return repr(value)


class QueryCacheScope(enum.Enum):
    """Whose results a cached query entry holds, and so who may be served it.

    The query bus keys an entry by the caller's identity: the tenant, organization and user of the
    ``ExecutionContext`` of ``query_with_context``, and the authenticated principal of the request
    (``RequestContext.security_context``). The ``X-Tenant-Id`` header is part of the key too, so it can only
    narrow an entry, but it never identifies a caller: any client can send it. The cache fails closed: a
    call whose caller the scope cannot identify is not cached. Declare a handler's scope with
    ``@cacheable(scope=...)`` from :mod:`pyfly.cqrs.cache.decorators`.
    """

    USER = "user"
    """Keyed by tenant, organization and user (the default): nobody is served another user's result. A call
    with no user (none in the context, no authenticated principal) is not cached."""

    TENANT = "tenant"
    """Keyed by the tenant and organization of the ``ExecutionContext``: the users of one tenant share
    entries. Without either in the context it is keyed by the user instead (the tenant may come from
    somewhere the cache cannot see, such as a claim of the principal, or only from the ``X-Tenant-Id``
    header), and a call with no user either is not cached."""

    GLOBAL = "global"
    """Not keyed by caller: every caller shares the entry, anonymous ones included. Only for data that is
    the same for everyone."""


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

        The key is ``<ClassName>:<digest>``. The digest is the full SHA-256 (:func:`cache_key_digest`) of the
        query class's module and qualified name and, for a dataclass, each field's name and the
        :func:`canonical_text` of its value. So:

        - two query classes with the same name in different modules (or nested in different scopes) get
          different keys;
        - the key does not change between processes and restarts when the fields' canonical text does not:
          plain values, and sets, dicts, lists, tuples and dataclasses of them (sets are sorted, dicts ordered;
          not the process-randomized built-in ``hash()``, audit #100). A field whose ``repr()`` depends on the
          process only costs misses;
        - two queries share a key only when their classes and the canonical text of every field are equal (a
          value whose ``repr()`` leaves out part of its state shares a key with values that differ only there).
          The digest is never truncated, so any other collision would be a SHA-256 collision: a caller cannot
          search for field values whose key is another query's, which matters because a ``GLOBAL`` entry is
          shared by every caller.

        A query that is not a dataclass is keyed by its class alone: every instance shares one key, whatever
        its attributes (override this method when they matter).

        The key names the query, not the caller: the bus adds the handler's ``cache_key_prefix`` and the
        caller's tenant and user (:class:`QueryCacheScope`), so do not put them in it yourself.
        """
        query_type = type(self)
        parts: list[str] = [query_type.__module__, query_type.__qualname__]
        if dataclasses.is_dataclass(self):
            fields = sorted((f.name, canonical_text(getattr(self, f.name))) for f in dataclasses.fields(self))
            parts += [part for field in fields for part in field]
        return f"{query_type.__name__}:{cache_key_digest(*parts)}"

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
