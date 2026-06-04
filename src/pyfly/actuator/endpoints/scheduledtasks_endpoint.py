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
"""Scheduled tasks actuator endpoint — Spring Boot ``/actuator/scheduledtasks`` parity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


def _runnable(cls_name: str, method: str) -> dict[str, str]:
    return {"target": f"{cls_name}.{method}"}


class ScheduledTasksEndpoint:
    """Exposes ``@scheduled`` tasks grouped by trigger type (cron/fixedDelay/fixedRate)."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "scheduledtasks"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        cron: list[dict[str, Any]] = []
        fixed_delay: list[dict[str, Any]] = []
        fixed_rate: list[dict[str, Any]] = []

        for cls, _reg in self._context.container._registrations.items():
            for attr_name in dir(cls):
                method = getattr(cls, attr_name, None)
                if method is None or not getattr(method, "__pyfly_scheduled__", False):
                    continue
                runnable = _runnable(cls.__name__, attr_name)
                cron_expr = getattr(method, "__pyfly_scheduled_cron__", None)
                fr = getattr(method, "__pyfly_scheduled_fixed_rate__", None)
                fd = getattr(method, "__pyfly_scheduled_fixed_delay__", None)
                initial = getattr(method, "__pyfly_scheduled_initial_delay__", None)

                if cron_expr:
                    cron.append({"runnable": runnable, "expression": cron_expr})
                elif fr is not None:
                    fixed_rate.append(
                        {
                            "runnable": runnable,
                            "interval": _to_millis(fr),
                            "initialDelay": _to_millis(initial),
                        }
                    )
                elif fd is not None:
                    fixed_delay.append(
                        {
                            "runnable": runnable,
                            "interval": _to_millis(fd),
                            "initialDelay": _to_millis(initial),
                        }
                    )

        return {"cron": cron, "fixedDelay": fixed_delay, "fixedRate": fixed_rate}


def _to_millis(value: Any) -> int | None:
    """Coerce a seconds/timedelta interval to milliseconds (Spring uses ms)."""
    if value is None:
        return None
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        return int(total_seconds() * 1000)
    try:
        return int(float(value) * 1000)
    except (TypeError, ValueError):
        return None
