# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Host-authoritative bounded facts, independent of transport and storage adapters."""

from typing import Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from pyfly.audit.models import AuditActor, AuditCorrelation, AuditResource, AuditScope, AuditUsage, Fact, Identifier


class AuditOperation(Fact):
    module: Identifier
    action: Identifier
    resource_kind: Identifier | None = None
    resource_key: Identifier | None = None

    @model_validator(mode="after")
    def bounded_event_type(self) -> Self:
        if len(self.module) + len(self.action) + 1 > 128:
            raise ValueError("Audit operation identity exceeds its bound")
        return self


class AuditContext(Fact):
    scope: AuditScope
    actor: AuditActor
    resource: AuditResource
    correlation: AuditCorrelation = Field(default_factory=AuditCorrelation)
    references: tuple[AuditResource, ...] = Field(default=(), max_length=16)
    reason_codes: tuple[Identifier, ...] = Field(default=(), max_length=16)
    usage: AuditUsage | None = None


@runtime_checkable
class AuditContextResolver(Protocol):
    async def resolve(self, operation: AuditOperation) -> AuditContext: ...
