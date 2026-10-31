# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Closed, immutable values for generic authorization policy constraints."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator


def _exact_identifier(value: str) -> str:
    if not value.isprintable() or any(char.isspace() for char in value):
        raise ValueError("Policy identifiers must be printable and contain no whitespace")
    return value


_Identifier = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=512), AfterValidator(_exact_identifier)
]
_Dimension = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=128), AfterValidator(_exact_identifier)
]
_Principals = Annotated[frozenset[_Identifier], Field(max_length=256)]
_Dimensions = Annotated[frozenset[_Dimension], Field(max_length=256)]


class _PolicyValue(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, validate_default=True, revalidate_instances="always"
    )


class PolicyLimits(_PolicyValue):
    """Optional host-enforced ceilings; ``None`` adds no ceiling and zero remains zero."""

    max_run_seconds: int | None = Field(default=None, strict=True, ge=0, le=86400)
    max_requests: int | None = Field(default=None, strict=True, ge=0, le=1000000)
    max_tokens: int | None = Field(default=None, strict=True, ge=0, le=1000000000)
    budget_microusd: int | None = Field(default=None, strict=True, ge=0, le=10**12)


class PolicySpec(_PolicyValue):
    """Version-one policy layer. Nullable allow sets are unrestricted; empty sets deny all."""

    schema_version: Literal[1] = 1
    allowed_principals: _Principals | None = None
    allowed_actions: _Dimensions | None = None
    allowed_target_kinds: _Dimensions | None = None
    approval_required_actions: _Dimensions = frozenset()
    min_approvals: int = Field(default=1, strict=True, ge=1, le=5)
    approval_ttl_seconds: int = Field(default=3600, strict=True, ge=300, le=86400)
    limits: PolicyLimits = Field(default_factory=PolicyLimits)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Policy schema_version must be the integer 1")
        return value


class PolicyContext(_PolicyValue):
    """Trusted host identity, operation, target kind and existing execution ceilings."""

    principal_id: _Identifier
    action: _Dimension
    target_kind: _Dimension
    limits: PolicyLimits = Field(default_factory=PolicyLimits)


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


class PolicyDecision(_PolicyValue):
    """Computed constraints, not an approval grant or a durable execution reservation."""

    outcome: PolicyOutcome
    reason_codes: tuple[_Dimension, ...] = ()
    effective_limits: PolicyLimits = Field(default_factory=PolicyLimits)
    min_approvals: int = Field(default=0, strict=True, ge=0, le=5)
    approval_ttl_seconds: int | None = Field(default=None, strict=True, ge=300, le=86400)
