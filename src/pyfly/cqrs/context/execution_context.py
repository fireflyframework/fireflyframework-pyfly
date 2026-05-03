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
"""Execution context propagated through the CQRS pipeline.

Mirrors Java's ``ExecutionContext`` interface & ``DefaultExecutionContext``
using a Protocol for structural sub-typing and a frozen dataclass for the
default implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecutionContext(Protocol):
    """Context propagated across the command/query pipeline.

    Carries user identity, tenant info, request metadata, feature flags,
    and arbitrary properties.
    """

    @property
    def user_id(self) -> str | None: ...

    @property
    def tenant_id(self) -> str | None: ...

    @property
    def organization_id(self) -> str | None: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def request_id(self) -> str | None: ...

    @property
    def source(self) -> str | None: ...

    @property
    def client_ip(self) -> str | None: ...

    @property
    def user_agent(self) -> str | None: ...

    @property
    def created_at(self) -> datetime: ...

    @property
    def properties(self) -> dict[str, Any]: ...

    @property
    def feature_flags(self) -> dict[str, bool]: ...

    def get_feature_flag(self, name: str, default: bool = False) -> bool: ...

    def get_property(self, key: str) -> Any | None: ...


@dataclass(frozen=True)
class DefaultExecutionContext:
    """Immutable, thread-safe default implementation of :class:`ExecutionContext`.

    Build instances via the :class:`ExecutionContextBuilder`.
    """

    user_id: str | None = None
    tenant_id: str | None = None
    organization_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    source: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    properties: dict[str, Any] = field(default_factory=dict)
    feature_flags: dict[str, bool] = field(default_factory=dict)

    def get_feature_flag(self, name: str, default: bool = False) -> bool:
        return self.feature_flags.get(name, default)

    def get_property(self, key: str) -> Any | None:
        return self.properties.get(key)


class ExecutionContextBuilder:
    """Fluent builder for :class:`DefaultExecutionContext`."""

    def __init__(self) -> None:
        self._user_id: str | None = None
        self._tenant_id: str | None = None
        self._organization_id: str | None = None
        self._session_id: str | None = None
        self._request_id: str | None = None
        self._source: str | None = None
        self._client_ip: str | None = None
        self._user_agent: str | None = None
        self._created_at: datetime | None = None
        self._properties: dict[str, Any] = {}
        self._feature_flags: dict[str, bool] = {}

    # ── identity ───────────────────────────────────────────────

    def with_user_id(self, user_id: str) -> ExecutionContextBuilder:
        self._user_id = user_id
        return self

    def with_tenant_id(self, tenant_id: str) -> ExecutionContextBuilder:
        self._tenant_id = tenant_id
        return self

    def with_organization_id(self, organization_id: str) -> ExecutionContextBuilder:
        self._organization_id = organization_id
        return self

    # ── request metadata ───────────────────────────────────────

    def with_session_id(self, session_id: str) -> ExecutionContextBuilder:
        self._session_id = session_id
        return self

    def with_request_id(self, request_id: str) -> ExecutionContextBuilder:
        self._request_id = request_id
        return self

    def with_source(self, source: str) -> ExecutionContextBuilder:
        self._source = source
        return self

    def with_client_ip(self, client_ip: str) -> ExecutionContextBuilder:
        self._client_ip = client_ip
        return self

    def with_user_agent(self, user_agent: str) -> ExecutionContextBuilder:
        self._user_agent = user_agent
        return self

    # ── extensibility ──────────────────────────────────────────

    def with_property(self, key: str, value: Any) -> ExecutionContextBuilder:
        self._properties[key] = value
        return self

    def with_feature_flag(self, name: str, enabled: bool = True) -> ExecutionContextBuilder:
        self._feature_flags[name] = enabled
        return self

    # ── build ──────────────────────────────────────────────────

    def build(self) -> DefaultExecutionContext:
        return DefaultExecutionContext(
            user_id=self._user_id,
            tenant_id=self._tenant_id,
            organization_id=self._organization_id,
            session_id=self._session_id,
            request_id=self._request_id,
            source=self._source,
            client_ip=self._client_ip,
            user_agent=self._user_agent,
            created_at=self._created_at or datetime.now(UTC),
            properties=dict(self._properties),
            feature_flags=dict(self._feature_flags),
        )
