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
"""Domain-layer exceptions.

These extend :class:`pyfly.kernel.BusinessException` so existing
exception handlers, RFC 7807 problem-details mappers, and error filters
already know how to translate them — but they live here for ergonomic
imports inside DDD aggregates and domain services.
"""

from __future__ import annotations

from typing import Any

from pyfly.kernel import BusinessException


class DomainException(BusinessException):
    """Base class for all domain-layer errors raised inside aggregates."""


class BusinessRuleViolation(DomainException):
    """A business invariant was violated.

    Use this for rules that are *intrinsic* to the domain — e.g. "an
    order cannot be cancelled after it has shipped", "transferring
    money requires both accounts to be active". For *input* validation
    (malformed request payloads) use
    :class:`pyfly.kernel.ValidationException`.
    """

    def __init__(
        self,
        rule: str,
        message: str | None = None,
        *,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message or f"Business rule violated: {rule}",
            code=code or "DOMAIN_RULE_VIOLATION",
            context={**(context or {}), "rule": rule},
        )
        self.rule = rule


class AggregateNotFound(DomainException):
    """A repository was asked for an aggregate that does not exist."""

    def __init__(self, aggregate_type: str, id: object) -> None:
        super().__init__(
            f"{aggregate_type} with id={id!r} not found",
            code="DOMAIN_AGGREGATE_NOT_FOUND",
            context={"aggregate_type": aggregate_type, "id": str(id)},
        )
        self.aggregate_type = aggregate_type
        self.aggregate_id = id
