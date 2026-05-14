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
"""Postgres-backed ``EventPublisher`` — durable outbox + LISTEN/NOTIFY.

The adapter persists every published event in a ``pyfly_eda_outbox``
table (append-only, monotonic ``BIGSERIAL`` id) and emits a
``pg_notify`` on a shared channel. Each consumer group keeps a row in
``pyfly_eda_offsets`` so consumers survive restarts and catch up on
events they missed.

Lifecycle ordering
==================

PyFly's :class:`ApplicationContext` auto-calls ``start()`` on every
bean that exposes ``start``/``stop`` methods, **before** application
code has a chance to call :meth:`subscribe`. The listener and the
consume loop therefore attach unconditionally at ``start()`` time;
:meth:`subscribe` simply appends a handler and pokes the consume
loop's wake event so newly registered handlers receive any events
that arrived in the meantime.

Delivery
========

* **At-least-once**: the cursor (``pyfly_eda_offsets.last_event_id``)
  is advanced **after** the handler has returned, not before. A crash
  mid-dispatch re-delivers from the last successful id.
* The consume loop also wakes on a fixed interval (``poll_interval_s``)
  so events arriving while a listener is reconnecting are not stuck.

Pgbouncer
=========

This adapter holds a long-lived ``LISTEN`` connection. Use a direct
DSN for ``listen_dsn`` (no pgbouncer in transaction-pooling mode) —
session-pooling or a dedicated direct connection is fine.

Requires ``asyncpg`` (``pip install pyfly[postgresql]`` or
``pip install pyfly[eda]``).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.types import EventEnvelope

logger = logging.getLogger(__name__)

_VALID_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _quote_ident(name: str) -> str:
    """Validate identifier (channel / group) for safe interpolation."""
    if not _VALID_IDENT.match(name):
        msg = f"invalid identifier: {name!r}"
        raise ValueError(msg)
    return name


def _normalise_dsn(dsn: str) -> str:
    """Strip SQLAlchemy dialect markers so asyncpg can parse the URL."""
    for marker in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres+asyncpg://"):
        if dsn.startswith(marker):
            return "postgresql://" + dsn[len(marker):]
    return dsn


_DDL_OUTBOX = """
CREATE TABLE IF NOT EXISTS pyfly_eda_outbox (
    id          BIGSERIAL PRIMARY KEY,
    destination TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    headers     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pyfly_eda_outbox_dest_idx
    ON pyfly_eda_outbox (destination, id);
"""

_DDL_OFFSETS = """
CREATE TABLE IF NOT EXISTS pyfly_eda_offsets (
    consumer_group TEXT PRIMARY KEY,
    last_event_id  BIGINT NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresEventBus:
    """``EventPublisher`` backed by Postgres LISTEN/NOTIFY + outbox table."""

    def __init__(
        self,
        *,
        dsn: str,
        listen_dsn: str | None = None,
        channel: str = "pyfly_eda",
        destinations: list[str] | None = None,
        group: str = "default",
        poll_interval_s: float = 5.0,
    ) -> None:
        # asyncpg only understands the bare ``postgresql://`` scheme; strip
        # SQLAlchemy-style dialect markers (``+asyncpg``, ``+psycopg``)
        # transparently so callers can reuse their ``DATABASE_URL``.
        self._dsn = _normalise_dsn(dsn)
        self._listen_dsn = _normalise_dsn(listen_dsn) if listen_dsn else self._dsn
        self._channel = _quote_ident(channel)
        self._destinations = list(destinations) if destinations else None
        self._group = group
        self._poll_interval_s = poll_interval_s
        self._handlers: list[tuple[str, EventHandler]] = []
        self._pool: Any = None
        self._listen_conn: Any = None
        self._consume_task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event = asyncio.Event()
        self._started = False
        self._closed = False

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        self._handlers.append((event_type_pattern, handler))
        # Always poke the consume loop. _drain() guards on the handler
        # list itself, so it's safe to wake even when start() hasn't
        # been called yet — the event simply persists until the loop
        # reaches its first wait_for().
        self._wake.set()

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self._started:
            await self.start()
        async with self._pool.acquire() as conn:
            event_id = await conn.fetchval(
                """
                INSERT INTO pyfly_eda_outbox (destination, event_type, payload, headers)
                VALUES ($1, $2, $3::jsonb, $4::jsonb)
                RETURNING id
                """,
                destination,
                event_type,
                json.dumps(payload),
                json.dumps(headers or {}),
            )
            # Postgres NOTIFY does NOT accept bind parameters; payload
            # has to be a string literal. event_id is BIGSERIAL, so a
            # plain int() cast is enough to keep this safe.
            await conn.execute(f"NOTIFY {self._channel}, '{int(event_id)}'")

    async def start(self) -> None:
        if self._started:
            return
        import asyncpg  # type: ignore[import-untyped]

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL_OUTBOX)
            await conn.execute(_DDL_OFFSETS)
            await conn.execute(
                """
                INSERT INTO pyfly_eda_offsets (consumer_group, last_event_id)
                VALUES ($1, 0)
                ON CONFLICT (consumer_group) DO NOTHING
                """,
                self._group,
            )

        # Attach the listener unconditionally — pyfly auto-starts adapter
        # beans before application code calls subscribe(), so we cannot
        # gate this on handlers existing. _drain() guards against
        # advancing the cursor while no handlers are registered.
        self._listen_conn = await asyncpg.connect(self._listen_dsn)
        await self._listen_conn.add_listener(self._channel, self._on_notify)

        self._closed = False
        self._wake.set()  # trigger an initial catch-up sweep
        self._consume_task = asyncio.create_task(self._consume_loop())
        self._started = True
        logger.info(
            "PostgresEventBus started: channel=%s destinations=%s group=%s",
            self._channel, self._destinations, self._group,
        )

    async def stop(self) -> None:
        self._closed = True
        self._started = False
        self._wake.set()  # wake the loop so it can observe _closed
        if self._consume_task is not None:
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
            self._consume_task = None
        if self._listen_conn is not None:
            try:
                await self._listen_conn.remove_listener(self._channel, self._on_notify)
            except Exception:
                logger.debug("listen connection already closed", exc_info=True)
            await self._listen_conn.close()
            self._listen_conn = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, _payload: str) -> None:
        # asyncpg invokes the listener from the event loop; flipping the
        # event is enough — the consumer loop does the actual fetch.
        self._wake.set()

    async def _consume_loop(self) -> None:
        # Yield once so the task scheduler can run any handler-registration
        # code that was queued during ``start()``. This makes the very
        # first ``_drain()`` call see the handler list as soon as
        # ``subscribe()`` has had a chance to append to it.
        await asyncio.sleep(0)
        try:
            while not self._closed:
                # Drain BEFORE waiting. _drain() is a no-op when no
                # handlers are registered, so this is cheap to call
                # eagerly. Doing it first guarantees an unconditional
                # catch-up sweep on startup without depending on the
                # _wake ordering.
                if self._handlers:
                    try:
                        await self._drain()
                    except Exception:
                        logger.exception("EDA Postgres drain loop failed")
                        await asyncio.sleep(0.5)
                if self._closed:
                    return
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_s)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
        except asyncio.CancelledError:
            pass

    async def _drain(self) -> None:
        # Don't touch the cursor while there's nothing to dispatch to —
        # otherwise events submitted before the worker subscribed would
        # be silently dropped.
        if not self._handlers:
            return
        while not self._closed:
            async with self._pool.acquire() as conn:
                offset = await conn.fetchval(
                    "SELECT last_event_id FROM pyfly_eda_offsets WHERE consumer_group = $1",
                    self._group,
                )
                if self._destinations:
                    rows = await conn.fetch(
                        """
                        SELECT id, destination, event_type, payload, headers, created_at
                        FROM pyfly_eda_outbox
                        WHERE id > $1 AND destination = ANY($2)
                        ORDER BY id
                        LIMIT 100
                        """,
                        offset,
                        self._destinations,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT id, destination, event_type, payload, headers, created_at
                        FROM pyfly_eda_outbox
                        WHERE id > $1
                        ORDER BY id
                        LIMIT 100
                        """,
                        offset,
                    )
            if not rows:
                return
            # Dispatch BEFORE advancing the cursor (at-least-once). If a
            # handler raises we stop early and the next drain retries
            # from the same point.
            last_dispatched = offset
            for row in rows:
                try:
                    await self._dispatch(row)
                except Exception:
                    logger.exception(
                        "Handler raised on event id=%s; deferring redelivery",
                        row["id"],
                    )
                    break
                last_dispatched = row["id"]
            if last_dispatched > offset:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE pyfly_eda_offsets
                        SET last_event_id = $1, updated_at = now()
                        WHERE consumer_group = $2 AND last_event_id < $1
                        """,
                        last_dispatched,
                        self._group,
                    )
            # If a handler crashed before processing every row in the
            # batch, back off briefly so we don't spin.
            if last_dispatched != rows[-1]["id"]:
                await asyncio.sleep(0.5)
                return

    async def _dispatch(self, row: Any) -> None:
        payload_raw = row["payload"]
        payload = payload_raw if isinstance(payload_raw, dict) else json.loads(payload_raw)
        headers_raw = row["headers"]
        headers = headers_raw if isinstance(headers_raw, dict) else json.loads(headers_raw or "{}")
        created_at = row["created_at"]
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        envelope = EventEnvelope(
            event_type=row["event_type"],
            payload=payload,
            destination=row["destination"],
            event_id=str(row["id"]),
            timestamp=created_at,
            headers=headers,
        )
        # Raise on handler failure so _drain() can leave the cursor at
        # the last successful id.
        for pattern, handler in self._handlers:
            if fnmatch.fnmatch(envelope.event_type, pattern):
                await handler(envelope)
