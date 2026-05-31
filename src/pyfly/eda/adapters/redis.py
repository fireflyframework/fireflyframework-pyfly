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
"""Redis Streams-backed ``EventPublisher`` — consumer-group fan-out.

Each ``destination`` is a stream key. Records are stored as
``{envelope: <serialized bytes>}`` so the wire format is independent
of the field naming. The consumer reads via ``XREADGROUP`` against the
configured consumer group, dispatches to handlers whose ``event_type``
pattern matches, then ``XACK``s on success.

Requires ``redis`` (``pip install pyfly[redis]``).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import socket
from typing import Any

from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.serializers import EventSerializer, JsonEventSerializer
from pyfly.eda.types import EventEnvelope

logger = logging.getLogger(__name__)


class RedisStreamsEventBus:
    """``EventPublisher`` backed by Redis Streams + consumer groups.

    Parameters
    ----------
    url:
        Redis connection URL (``redis://`` or ``rediss://``).
    streams:
        Streams the consumer reads from. Each stream is a destination.
        Defaults to ``["pyfly.events"]``.
    group:
        Consumer group name. Defaults to ``"pyfly-default"``.
    consumer_id:
        Stable consumer identifier inside the group; defaults to the
        hostname. Used by ``XREADGROUP`` for pending-entry tracking.
    block_ms:
        ``XREADGROUP`` long-poll timeout. Defaults to 5000 ms.
    serializer:
        Envelope serializer; defaults to ``JsonEventSerializer``.
    """

    def __init__(
        self,
        *,
        url: str,
        streams: list[str] | None = None,
        group: str = "pyfly-default",
        consumer_id: str | None = None,
        block_ms: int = 5000,
        serializer: EventSerializer | None = None,
    ) -> None:
        from redis import asyncio as redis_asyncio

        self._url = url
        self._streams = list(streams) if streams else ["pyfly.events"]
        self._group = group
        self._consumer_id = consumer_id or socket.gethostname()
        self._block_ms = block_ms
        self._serializer: EventSerializer = serializer or JsonEventSerializer()
        self._client = redis_asyncio.Redis.from_url(url, decode_responses=False)
        self._handlers: list[tuple[str, EventHandler]] = []
        self._consume_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        self._handlers.append((event_type_pattern, handler))

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self._started:
            await self.start()
        envelope = EventEnvelope(
            event_type=event_type,
            payload=payload,
            destination=destination,
            headers=headers or {},
        )
        await self._client.xadd(destination, {b"envelope": self._serializer.serialize(envelope)})

    async def start(self) -> None:
        if self._started:
            return
        for stream in self._streams:
            try:
                await self._client.xgroup_create(
                    name=stream,
                    groupname=self._group,
                    id="$",
                    mkstream=True,
                )
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        # Always start the consume loop — pyfly's ApplicationContext
        # auto-starts adapter beans before application code calls
        # subscribe(), so gating on handlers would lose every event
        # published between start() and the first subscribe().
        self._closed = False
        self._consume_task = asyncio.create_task(self._consume_loop())
        self._started = True

    async def stop(self) -> None:
        self._closed = True
        self._started = False
        if self._consume_task is not None:
            self._consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consume_task
            self._consume_task = None
        await self._client.close()

    async def _consume_loop(self) -> None:
        streams: dict[Any, Any] = {s: ">" for s in self._streams}
        try:
            while not self._closed:
                try:
                    response = await self._client.xreadgroup(
                        groupname=self._group,
                        consumername=self._consumer_id,
                        streams=streams,
                        count=10,
                        block=self._block_ms,
                    )
                except Exception:
                    logger.exception("Redis xreadgroup failed; sleeping before retry")
                    await asyncio.sleep(1.0)
                    continue
                if not response:
                    continue
                for stream_key, entries in response:
                    stream_name = stream_key.decode() if isinstance(stream_key, bytes) else stream_key
                    for entry_id, fields in entries:
                        await self._dispatch_and_ack(stream_name, entry_id, fields)
        except asyncio.CancelledError:
            pass

    async def _dispatch_and_ack(
        self,
        stream_name: str,
        entry_id: bytes | str,
        fields: dict[bytes | str, bytes | str],
    ) -> None:
        raw = fields.get(b"envelope") or fields.get("envelope")
        if raw is None:
            logger.warning(
                "Stream entry %s/%s missing 'envelope' field; acking and skipping",
                stream_name,
                entry_id,
            )
            await self._client.xack(stream_name, self._group, entry_id)
            return
        try:
            envelope = self._serializer.deserialize(raw if isinstance(raw, bytes) else raw.encode())
        except Exception:
            logger.exception(
                "Failed to deserialize envelope for %s/%s; acking to prevent redelivery",
                stream_name,
                entry_id,
            )
            await self._client.xack(stream_name, self._group, entry_id)
            return
        delivered = False
        for pattern, handler in self._handlers:
            if fnmatch.fnmatch(envelope.event_type, pattern):
                delivered = True
                try:
                    await handler(envelope)
                except Exception:
                    logger.exception(
                        "Handler for pattern=%s raised on event_type=%s; leaving entry %s unacked for re-delivery",
                        pattern,
                        envelope.event_type,
                        entry_id,
                    )
                    return
        if not delivered:
            # No handler matched; ack so we don't redeliver forever.
            logger.debug(
                "No handler matched event_type=%s on %s; acking",
                envelope.event_type,
                stream_name,
            )
        await self._client.xack(stream_name, self._group, entry_id)
