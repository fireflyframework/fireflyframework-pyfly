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
"""CQRS authorization result types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AuthorizationSeverity(StrEnum):
    """Severity level for an authorization error."""

    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class AuthorizationError:
    """A single authorization failure."""

    resource: str
    message: str
    error_code: str = "AUTHORIZATION_ERROR"
    severity: AuthorizationSeverity = AuthorizationSeverity.ERROR
    denied_action: str | None = None


@dataclass(frozen=True)
class AuthorizationResult:
    """Immutable authorization decision.

    Compose multiple results with :meth:`combine`.
    """

    authorized: bool
    errors: tuple[AuthorizationError, ...] = ()
    summary: str | None = None

    # ── factories ──────────────────────────────────────────────

    @staticmethod
    def success() -> AuthorizationResult:
        return AuthorizationResult(authorized=True)

    @staticmethod
    def failure(
        resource: str,
        message: str,
        *,
        error_code: str = "AUTHORIZATION_ERROR",
        denied_action: str | None = None,
    ) -> AuthorizationResult:
        return AuthorizationResult(
            authorized=False,
            errors=(
                AuthorizationError(
                    resource=resource,
                    message=message,
                    error_code=error_code,
                    denied_action=denied_action,
                ),
            ),
        )

    # ── combinators ────────────────────────────────────────────

    def combine(self, other: AuthorizationResult) -> AuthorizationResult:
        """Merge two results; unauthorized if either is unauthorized."""
        return AuthorizationResult(
            authorized=self.authorized and other.authorized,
            errors=self.errors + other.errors,
        )

    # ── helpers ────────────────────────────────────────────────

    def error_messages(self) -> list[str]:
        return [f"{e.resource}: {e.message}" for e in self.errors]
