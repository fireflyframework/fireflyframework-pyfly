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
"""Info actuator endpoint — application name, version, description."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class InfoEndpoint:
    """Exposes application metadata at ``/actuator/info``."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "info"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        return {
            "app": {
                "name": self._context.config.get("pyfly.app.name", ""),
                "version": self._context.config.get("pyfly.app.version", ""),
                "description": self._context.config.get("pyfly.app.description", ""),
            },
        }
