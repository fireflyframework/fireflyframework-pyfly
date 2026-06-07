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
"""ContextRefresher — Spring Cloud's runtime configuration refresh.

Evicts all refresh-scoped beans and resets ``@config_properties`` beans so the next
resolution rebuilds them against the live ``Config`` (which re-reads environment variables
and ``${...}`` placeholders at access time), then publishes a
:class:`~pyfly.context.events.RefreshScopeRefreshedEvent`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.context.events import RefreshScopeRefreshedEvent

if TYPE_CHECKING:
    from pyfly.container.container import Container
    from pyfly.container.refresh_scope import RefreshScope
    from pyfly.context.events import ApplicationEventBus


class ContextRefresher:
    """Triggers a refresh of refresh-scoped and ``@config_properties`` beans."""

    def __init__(self, container: Container, scope: RefreshScope, event_bus: ApplicationEventBus) -> None:
        self._container = container
        self._scope = scope
        self._event_bus = event_bus

    async def refresh(self) -> list[str]:
        """Evict refresh-scoped beans, reset config-properties beans, publish the event.

        Returns the cache keys of the evicted refresh-scoped beans.
        """
        evicted = self._scope.refresh()
        # Reset @config_properties singletons so they re-bind from the live Config on next
        # resolution (their factory is ``lambda: config.bind(cls)`` — see
        # ApplicationContext._bind_config_properties).
        for cls, reg in list(self._container._registrations.items()):
            if hasattr(cls, "__pyfly_config_prefix__") and reg.factory is not None:
                reg.instance = None
        await self._event_bus.publish(RefreshScopeRefreshedEvent(evicted))
        return evicted
