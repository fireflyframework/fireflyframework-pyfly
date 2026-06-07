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
    from pyfly.core.config import Config


class ContextRefresher:
    """Triggers a refresh of refresh-scoped and ``@config_properties`` beans."""

    def __init__(
        self,
        container: Container,
        scope: RefreshScope,
        event_bus: ApplicationEventBus,
        config: Config | None = None,
    ) -> None:
        self._container = container
        self._scope = scope
        self._event_bus = event_bus
        self._config = config

    async def refresh(self) -> list[str]:
        """Reload config from sources, evict refresh-scoped beans, reset config-properties
        beans, and publish the event.

        Returns the cache keys of the evicted refresh-scoped beans.
        """
        # 1. Re-read the config sources so rebuilt beans pick up file/profile changes
        # (no-op for dict-constructed config).
        if self._config is not None:
            self._config.reload_from_sources()
        evicted = self._scope.refresh()
        # Reset @config_properties singletons so they re-bind from the live Config on next
        # resolution (their factory is ``lambda: config.bind(cls)`` — see
        # ApplicationContext._bind_config_properties).
        for cls in self._container.registered_types():
            reg = self._container.get_registration(cls)
            if reg is not None and hasattr(cls, "__pyfly_config_prefix__") and reg.factory is not None:
                self._container.reset_instance(cls)
        await self._event_bus.publish(RefreshScopeRefreshedEvent(evicted))
        return evicted
