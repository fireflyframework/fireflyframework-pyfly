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
"""``HealthIndicator`` for any :class:`EventPublisher`.

Strategy is broker-aware via duck typing:

* If the publisher exposes a ``ping()`` coroutine, call it.
* Else, if it exposes a ``_started`` boolean (matches every adapter in
  ``pyfly.eda.adapters``), surface that.
* Else, return ``UP`` (the in-memory bus has no failure mode).

This keeps the indicator broker-agnostic. For a deeper check, register
a dedicated indicator for the specific adapter alongside this one.
"""

from __future__ import annotations

from pyfly.actuator.health import HealthStatus
from pyfly.eda.ports.outbound import EventPublisher


class EventPublisherHealthIndicator:
    """Generic ``HealthIndicator`` over the :class:`EventPublisher` port."""

    def __init__(self, publisher: EventPublisher) -> None:
        self._publisher = publisher

    async def health(self) -> HealthStatus:
        ping = getattr(self._publisher, "ping", None)
        if callable(ping):
            try:
                await ping()
            except Exception as exc:
                return HealthStatus(
                    status="DOWN",
                    details={"error": type(exc).__name__, "message": str(exc)[:200]},
                )
            return HealthStatus(status="UP", details={"adapter": type(self._publisher).__name__})

        started = getattr(self._publisher, "_started", None)
        if started is False:
            return HealthStatus(
                status="DOWN",
                details={"adapter": type(self._publisher).__name__, "reason": "not started"},
            )
        return HealthStatus(status="UP", details={"adapter": type(self._publisher).__name__})
