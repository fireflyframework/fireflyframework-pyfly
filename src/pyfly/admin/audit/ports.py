# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Host-owned authority; neither query strings nor writer contexts grant read access."""

from typing import Protocol, runtime_checkable

from pyfly.audit.models import AuditScope
from pyfly.security.context import SecurityContext


@runtime_checkable
class AdminAuditScopeResolver(Protocol):
    async def resolve(self, security: SecurityContext, requested_handle: str | None) -> AuditScope:
        """Recheck current authority and resolve an opaque handle to one exact allowed scope."""
        ...
