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
"""Feature flag management for the admin dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext
    from pyfly.feature_flags.management import FlagManagement


class FlagsProvider:
    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    def _management(self) -> FlagManagement | None:
        try:
            from pyfly.feature_flags.endpoint import _management
        except ImportError:
            return None

        return _management(self._context)

    async def get_flags(self) -> dict[str, Any]:
        management = self._management()
        return {"available": False} if management is None else {"available": True, **await management.overview()}

    async def get_flag(self, key: str) -> dict[str, Any] | None:
        management = self._management()
        return None if management is None else await management.detail(key)

    async def execute(self, key: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        management = self._management()
        if management is None:
            return 409, {"error": "not-writable", "message": "feature flags are not enabled"}
        from pyfly.feature_flags.management import FlagManagementError, actor_from_security

        try:
            return 200, await management.execute(key, body, actor=actor_from_security("admin"))
        except FlagManagementError as error:
            return error.status, error.to_body()
