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
"""CQRS authorization exceptions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.kernel.exceptions import ForbiddenException

if TYPE_CHECKING:
    from pyfly.cqrs.authorization.types import AuthorizationResult


class AuthorizationException(ForbiddenException):
    """Raised when command/query authorization fails."""

    def __init__(self, result: AuthorizationResult, message: str | None = None) -> None:
        self.result = result
        summary = message or "; ".join(result.error_messages()) or "Authorization denied"
        super().__init__(
            message=summary,
            code="AUTHORIZATION_DENIED",
            context={"errors": [{"resource": e.resource, "message": e.message} for e in result.errors]},
        )
