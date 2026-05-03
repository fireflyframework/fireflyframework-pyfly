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
"""Scheduled tasks data provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class ScheduledProvider:
    """Provides scheduled task metadata."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    async def get_scheduled_tasks(self) -> dict[str, Any]:
        tasks: list[dict[str, Any]] = []
        for cls, reg in self._context.container._registrations.items():
            if reg.instance is None:
                continue
            for attr_name in dir(cls):
                method = getattr(cls, attr_name, None)
                if method is None:
                    continue
                if not getattr(method, "__pyfly_scheduled__", False):
                    continue
                tasks.append(
                    {
                        "class": cls.__name__,
                        "method": attr_name,
                        "cron": getattr(method, "__pyfly_scheduled_cron__", None),
                        "fixed_rate": str(getattr(method, "__pyfly_scheduled_fixed_rate__", None)),
                        "fixed_delay": str(getattr(method, "__pyfly_scheduled_fixed_delay__", None)),
                        "initial_delay": str(getattr(method, "__pyfly_scheduled_initial_delay__", None)),
                    }
                )
        return {"tasks": tasks, "total": len(tasks)}
