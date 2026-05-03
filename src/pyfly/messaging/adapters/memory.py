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
"""In-memory message broker for testing and single-process applications."""

from __future__ import annotations

import asyncio
import itertools

from pyfly.messaging.ports.outbound import MessageHandler
from pyfly.messaging.types import Message


class InMemoryMessageBroker:
    def __init__(self) -> None:
        self._subscriptions: dict[str, list[tuple[MessageHandler, str | None]]] = {}
        self._group_iterators: dict[tuple[str, str], itertools.cycle[MessageHandler]] = {}
        self._running = False
        self._lock = asyncio.Lock()

    async def publish(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self._running:
            raise RuntimeError("Broker is not running")
        msg = Message(topic=topic, value=value, key=key, headers=headers or {})
        async with self._lock:
            subs = list(self._subscriptions.get(topic, []))
        delivered_groups: set[str] = set()
        for handler, group in subs:
            if group is None:
                await handler(msg)
            elif group not in delivered_groups:
                delivered_groups.add(group)
                async with self._lock:
                    rr_key = (topic, group)
                    if rr_key not in self._group_iterators:
                        group_handlers = [h for h, g in subs if g == group]
                        self._group_iterators[rr_key] = itertools.cycle(group_handlers)
                    selected = next(self._group_iterators[rr_key])
                await selected(msg)

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        group: str | None = None,
    ) -> None:
        async with self._lock:
            if topic not in self._subscriptions:
                self._subscriptions[topic] = []
            self._subscriptions[topic].append((handler, group))
            if group is not None:
                rr_key = (topic, group)
                self._group_iterators.pop(rr_key, None)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
