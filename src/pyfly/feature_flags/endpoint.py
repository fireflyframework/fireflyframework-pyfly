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
"""The flags actuator endpoint, resolving management after the context starts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyfly.actuator.ports import write_operation
from pyfly.feature_flags.management import FlagManagement, FlagManagementError, actor_from_security

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext

__all__ = ["FlagsEndpoint", "flags_endpoint_for"]

_NOT_STARTED: dict[str, Any] = {"provider": None, "writable": False, "writesEnabled": False, "sources": [], "flags": []}


def _management(context: ApplicationContext) -> FlagManagement | None:
    for registration in context.container._registrations.values():
        if isinstance(registration.instance, FlagManagement):
            return registration.instance
    return None


class FlagsEndpoint:
    """GET lists and inspects flags; POST runs management actions."""

    supports_selector = True
    selector_not_found_error = "unknown-flag"
    invalid_body_error = "bad-request"

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "flags"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any] | None:
        selector = context.get("selector") if isinstance(context, dict) else None
        management = _management(self._context)
        if selector:
            return await management.detail(selector) if management is not None else None
        return await management.overview() if management is not None else dict(_NOT_STARTED)

    @write_operation
    async def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
        key = context.get("selector")
        if not key:
            return FlagManagementError("bad-request", "POST /actuator/flags/{key} with an action").to_body()
        management = _management(self._context)
        if management is None:
            return FlagManagementError("not-writable", "feature flags are not started").to_body()
        try:
            return await management.execute(str(key), body, actor=actor_from_security("actuator"))
        except FlagManagementError as error:
            return error.to_body()


def flags_endpoint_for(context: ApplicationContext) -> FlagsEndpoint | None:
    """Create the endpoint only when feature flags are enabled."""
    enabled = str(context.config.get("pyfly.feature-flags.enabled", "false")).lower() in ("true", "1", "yes")
    return FlagsEndpoint(context) if enabled else None
