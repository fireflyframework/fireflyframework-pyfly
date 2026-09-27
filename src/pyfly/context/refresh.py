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
and ``${...}`` placeholders at access time), destroys the evicted instances (their
``@pre_destroy``, the destroy method of a ``@bean`` product, ``stop()`` of a lifecycle bean, so a
refresh-scoped bean that owns an engine disposes it), then publishes a
:class:`~pyfly.context.events.RefreshScopeRefreshedEvent`.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pyfly.context.events import RefreshScopeRefreshedEvent

if TYPE_CHECKING:
    from pyfly.container.container import Container
    from pyfly.container.refresh_scope import RefreshScope
    from pyfly.context.events import ApplicationEventBus
    from pyfly.core.config import Config

_logger = logging.getLogger(__name__)


async def _run_pre_destroy(key: str, instance: Any) -> None:
    """Call the ``@pre_destroy`` methods of *instance*; a failure is logged, not raised."""
    del key
    for attr_name in dir(type(instance)):
        member = inspect.getattr_static(type(instance), attr_name, None)
        if not getattr(member, "__pyfly_pre_destroy__", False):
            continue
        try:
            result = getattr(instance, attr_name)()
            if inspect.isawaitable(result):
                await result
        except Exception:
            _logger.warning(
                "pre_destroy_failed", extra={"bean": type(instance).__qualname__, "method": attr_name}, exc_info=True
            )


class ContextRefresher:
    """Triggers a refresh of refresh-scoped and ``@config_properties`` beans.

    *destroy* destroys one evicted instance, given its scope key; the ``ApplicationContext`` passes
    its own (``@pre_destroy``, the ``@bean``'s destroy method, ``stop()``, each within the shutdown
    timeout). Without it the ``@pre_destroy`` methods are called directly.
    """

    def __init__(
        self,
        container: Container,
        scope: RefreshScope,
        event_bus: ApplicationEventBus,
        config: Config | None = None,
        *,
        destroy: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> None:
        self._container = container
        self._scope = scope
        self._event_bus = event_bus
        self._config = config
        self._destroy = destroy or _run_pre_destroy

    async def refresh(self) -> list[str]:
        """Reload config from sources, evict refresh-scoped beans, reset config-properties
        beans, destroy the evicted instances, and publish the event.

        Returns the cache keys of the evicted refresh-scoped beans.

        The evicted instances are destroyed after the swap: the scope already hands out new ones, so
        a new request gets the rebuilt bean while the evicted one is closing. Work that still holds
        the evicted instance finishes on it (``AsyncEngine.dispose()`` lets a checked-out connection
        finish and closes it when it is returned). Until 26.09.07 nothing destroyed them: each
        refresh of a bean that owned an engine leaked that engine's pool. Destroying runs the
        ``@pre_destroy`` methods, the destroy method of a ``@bean`` product (a refresh-scoped
        ``AsyncEngine`` bean is disposed) and ``stop()`` of a lifecycle bean.
        """
        # 1. Re-read the config sources so rebuilt beans pick up file/profile changes
        # (no-op for dict-constructed config).
        if self._config is not None:
            self._config.reload_from_sources()
        evicted = self._scope.evict_all()
        # Reset @config_properties singletons so they re-bind from the live Config on next
        # resolution (their factory is ``lambda: config.bind(cls)`` — see
        # ApplicationContext._bind_config_properties).
        for cls in self._container.registered_types():
            reg = self._container.get_registration(cls)
            if reg is not None and hasattr(cls, "__pyfly_config_prefix__") and reg.factory is not None:
                self._container.reset_instance(cls)
        # The most recently created first: a scoped bean is cached after the scoped beans it uses.
        for key, instance in reversed(list(evicted.items())):
            await self._destroy(key, instance)
        keys = list(evicted)
        await self._event_bus.publish(RefreshScopeRefreshedEvent(keys))
        return keys
