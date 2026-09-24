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
"""EDA → CQRS cache-invalidation bridge.

The :class:`EventDrivenCacheInvalidator` works against Python objects
(``type(event)``), but the EDA bus delivers :class:`~pyfly.eda.types.EventEnvelope`
objects whose identity is the *string* ``event_type`` field.  This module
provides :class:`EdaCacheInvalidationBridge` which registers rules by
**event-type string** and resolves ``{field}`` placeholders from the
envelope's ``payload`` dict.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.eda.types import EventEnvelope

if TYPE_CHECKING:
    from pyfly.eda.ports.outbound import EventPublisher

_logger = logging.getLogger(__name__)


class EdaCacheInvalidationBridge:
    """Evicts CQRS cache entries when EDA events arrive.

    Rules map an *event-type string* (as carried in the
    :class:`~pyfly.eda.types.EventEnvelope`) to one or more cache-key
    patterns.  Patterns may contain ``{field}`` placeholders that are
    resolved from the envelope's ``payload`` dict.

    Typical usage::

        bridge = EdaCacheInvalidationBridge(cache_adapter)
        bridge.register("order.updated", "order:{order_id}")
        bridge.subscribe(event_bus)   # subscribes "order.updated", and nothing else

    A bridge with no registered rule subscribes to nothing at all, so a process
    that invalidates no cache entry is not a consumer of the bus.

    Then, when the EDA bus delivers an event of type ``"order.updated"``
    with payload ``{"order_id": "42", ...}``, the bridge evicts the cache
    key ``order:42``.
    """

    def __init__(self, cache: QueryCacheAdapter) -> None:
        self._cache = cache
        self._rules: dict[str, list[str]] = {}
        self._publisher: EventPublisher | None = None
        self._subscribed: set[str] = set()

    # ── rule registration ──────────────────────────────────────

    def register(self, event_type: str, cache_key_pattern: str) -> None:
        """Register a cache-key pattern to evict when *event_type* arrives.

        When the bridge is already attached to a bus, registering a rule also
        subscribes that one event type — the bridge only ever consumes what it
        has something to do about.

        Args:
            event_type: The EDA event-type string (e.g. ``"order.updated"``).
            cache_key_pattern: A pattern such as ``"order:{order_id}"`` where
                ``{field}`` placeholders are resolved from the envelope payload.
        """
        self._rules.setdefault(event_type, []).append(cache_key_pattern)
        self._subscribe_event_type(event_type)

    # ── EDA subscription ───────────────────────────────────────

    def subscribe(self, event_publisher: EventPublisher) -> None:
        """Attach this bridge to the EDA bus.

        Subscribes :meth:`on_envelope` **once per registered event type**, and
        nothing at all while no rule is registered.

        It used to subscribe the wildcard ``"*"``, which made every process a
        consuming member of the group in order to discard what it received. On
        the Postgres bus that is not passive: the adapter refuses to advance its
        cursor while no handler is registered, precisely so events published
        before a worker subscribed are not lost — and a wildcard handler that
        dispatches nothing defeated that guard, draining another process's queue
        into a no-op. On Kafka and Redis it cost a partition assignment and a
        pattern subscription for no work.

        Attaching before or after :meth:`register` both work: rules registered
        later subscribe themselves.

        Args:
            event_publisher: The live EDA bus (e.g. an
                :class:`~pyfly.eda.adapters.memory.InMemoryEventBus`).
        """
        self._publisher = event_publisher
        for event_type in self._rules:
            self._subscribe_event_type(event_type)

    def _subscribe_event_type(self, event_type: str) -> None:
        if self._publisher is None or event_type in self._subscribed:
            return
        self._publisher.subscribe(event_type, self.on_envelope)
        self._subscribed.add(event_type)

    # ── event handler ──────────────────────────────────────────

    async def on_envelope(self, envelope: EventEnvelope) -> None:
        """Handle an incoming EDA envelope.

        Looks up registered rules for ``envelope.event_type``, resolves each
        pattern against ``envelope.payload``, and evicts matching cache keys
        via the :class:`~pyfly.cqrs.cache.adapter.QueryCacheAdapter`.

        Args:
            envelope: The :class:`~pyfly.eda.types.EventEnvelope` delivered by
                the EDA bus.
        """
        patterns = self._rules.get(envelope.event_type, [])
        for pattern in patterns:
            cache_key = self._resolve_pattern(pattern, envelope.payload)
            evicted = await self._cache.evict(cache_key)
            if evicted:
                _logger.debug(
                    "Cache evicted key '%s' on EDA event '%s'",
                    cache_key,
                    envelope.event_type,
                )

    # ── internal helpers ───────────────────────────────────────

    @staticmethod
    def _resolve_pattern(pattern: str, payload: dict[str, object]) -> str:
        """Resolve ``{field}`` placeholders in *pattern* from *payload*.

        Unresolvable placeholders are left as-is and a warning is logged.

        Args:
            pattern: The cache-key pattern, e.g. ``"order:{order_id}"``.
            payload: The envelope's payload dict.

        Returns:
            The resolved cache key string.
        """

        def _replace(match: re.Match[str]) -> str:
            field_name = match.group(1)
            value = payload.get(field_name)
            if value is None:
                _logger.warning(
                    "Cache invalidation pattern field '%s' not found in payload for pattern '%s'",
                    field_name,
                    pattern,
                )
                return match.group(0)
            return str(value)

        return re.sub(r"\{(\w+)\}", _replace, pattern)
