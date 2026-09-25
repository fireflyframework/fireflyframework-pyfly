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
"""SQLAlchemy query and pool metrics — R2dbcMetrics / HikariCP metrics parity.

:class:`SqlAlchemyQueryMetrics` attaches SQLAlchemy core event listeners to an engine's
``sync_engine`` to record per-operation query duration (histogram), query count (counter), and
query errors (counter) via a :class:`~pyfly.observability.ports.MetricsRecorder`. The ``operation``
label is restricted to ``{SELECT, INSERT, UPDATE, DELETE, OTHER}`` so Prometheus cardinality stays
bounded regardless of query shape.

:class:`SqlAlchemyPoolMetrics` exports each datasource's connection pool (labeled ``datasource``):
configured size, connections checked out, idle connections, overflow in use, and invalidated
connections. The relational auto-configuration binds both to every engine of the datasource registry.
"""

from __future__ import annotations

import time
from typing import Any

from pyfly.observability.ports import MetricsRecorder

_KNOWN_OPS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})


def _operation(statement: str) -> str:
    """Return the leading SQL verb, normalised to a bounded set of labels.

    Only ``SELECT``, ``INSERT``, ``UPDATE``, and ``DELETE`` are kept as-is;
    everything else — including DDL, ``CALL``, ``MERGE``, empty strings, etc. —
    maps to ``OTHER``.
    """
    first = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
    return first if first in _KNOWN_OPS else "OTHER"


class SqlAlchemyQueryMetrics:
    """Instruments a SQLAlchemy :class:`~sqlalchemy.ext.asyncio.AsyncEngine` with metrics.

    Registers ``before_cursor_execute``, ``after_cursor_execute``, and
    ``handle_error`` event listeners on ``engine.sync_engine`` so every
    statement execution is measured regardless of whether the caller uses the
    ORM or Core.

    The three metric handles are created in ``__init__`` so the recorder is only
    called once at construction time (idempotent wrt registry registration).

    Parameters
    ----------
    engine:
        A SQLAlchemy :class:`~sqlalchemy.ext.asyncio.AsyncEngine` (or anything
        that exposes a ``sync_engine`` attribute accepted by ``sqlalchemy.event``).
    recorder:
        The active :class:`~pyfly.observability.ports.MetricsRecorder` to use.
    """

    def __init__(self, engine: Any, recorder: MetricsRecorder) -> None:
        self._engine = engine
        self._attached = False

        self._duration: Any = recorder.histogram(
            "pyfly_db_query_duration_seconds",
            "Database query execution time",
            labels=["operation"],
        )
        self._count: Any = recorder.counter(
            "pyfly_db_queries_total",
            "Database queries executed",
            labels=["operation"],
        )
        self._errors: Any = recorder.counter(
            "pyfly_db_query_errors_total",
            "Database query errors",
            labels=["operation"],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """Attach event listeners to the engine's ``sync_engine``.

        Idempotent — subsequent calls after the first are no-ops.
        """
        if self._attached:
            return

        from sqlalchemy import event

        sync = self._engine.sync_engine
        event.listen(sync, "before_cursor_execute", self._before_cursor_execute)
        event.listen(sync, "after_cursor_execute", self._after_cursor_execute)
        event.listen(sync, "handle_error", self._handle_error)
        self._attached = True

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _before_cursor_execute(
        self,
        conn: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        """Stash a high-resolution start timestamp on the execution context."""
        context._pyfly_query_start = time.perf_counter()

    def _after_cursor_execute(
        self,
        conn: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        """Record count and duration for a successfully completed statement."""
        op = _operation(statement or "")
        self._count.labels(operation=op).inc()
        start: float | None = getattr(context, "_pyfly_query_start", None)
        if start is not None:
            self._duration.labels(operation=op).observe(time.perf_counter() - start)

    def _handle_error(self, exception_context: Any) -> None:
        """Increment the error counter when a statement raises a DB-level error."""
        op = _operation(exception_context.statement or "")
        self._errors.labels(operation=op).inc()


class SqlAlchemyPoolMetrics:
    """Exports connection-pool gauges per datasource, the HikariCP metrics equivalent.

    ============================================  ===========================================
    Metric (label ``datasource``)                 Meaning
    ============================================  ===========================================
    ``pyfly_db_pool_size``                        Configured pool size
    ``pyfly_db_pool_checked_out``                 Connections in use
    ``pyfly_db_pool_idle``                        Connections idle in the pool
    ``pyfly_db_pool_overflow``                    Overflow connections in use (0 when none)
    ``pyfly_db_pool_invalidated_total``           Connections invalidated (disconnects, errors)
    ============================================  ===========================================

    With the Prometheus recorder the gauges are read from the pool at scrape time, so they are exact
    at rest and under load; with a recorder whose gauges have no ``set_function`` they are refreshed on
    every checkout and checkin. The gauges cover queue pools (every server database and SQLite files);
    the in-memory SQLite ``StaticPool`` has one connection and reports invalidations only. The
    datasource label is the registry's ``qualified_name`` (``primary``, ``primary.replica``, ...).
    """

    def __init__(self, recorder: MetricsRecorder) -> None:
        self._size: Any = recorder.gauge("pyfly_db_pool_size", "Configured connection pool size", labels=["datasource"])
        self._checked_out: Any = recorder.gauge(
            "pyfly_db_pool_checked_out", "Pooled connections in use", labels=["datasource"]
        )
        self._idle: Any = recorder.gauge("pyfly_db_pool_idle", "Pooled connections idle", labels=["datasource"])
        self._overflow: Any = recorder.gauge(
            "pyfly_db_pool_overflow", "Overflow connections in use", labels=["datasource"]
        )
        self._invalidated: Any = recorder.counter(
            "pyfly_db_pool_invalidated_total", "Pooled connections invalidated", labels=["datasource"]
        )
        self._bound: set[tuple[str, int]] = set()

    def bind(self, datasource: str, engine: Any) -> None:
        """Export *engine*'s pool under the label *datasource* (idempotent per engine and label)."""
        key = (datasource, id(engine))
        if key in self._bound:
            return
        self._bound.add(key)

        from sqlalchemy import event

        sync_engine = engine.sync_engine
        readings: dict[Any, Any] = {
            self._size: lambda: _pool_stat(sync_engine, "size"),
            self._checked_out: lambda: _pool_stat(sync_engine, "checkedout"),
            self._idle: lambda: _pool_stat(sync_engine, "checkedin"),
            self._overflow: lambda: max(_pool_stat(sync_engine, "overflow"), 0),
        }
        live = True
        for gauge, reading in readings.items():
            child = gauge.labels(datasource=datasource)
            set_function = getattr(child, "set_function", None)
            if callable(set_function):
                set_function(reading)
            else:
                live = False
        if not live:

            def _refresh(*_args: Any) -> None:
                for gauge, reading in readings.items():
                    setter = getattr(gauge.labels(datasource=datasource), "set", None)
                    if callable(setter):  # a recorder without settable gauges records nothing here
                        setter(reading())

            for name in ("connect", "checkout", "checkin", "close", "detach"):
                event.listen(sync_engine, name, _refresh)
            _refresh()

        invalidated = self._invalidated.labels(datasource=datasource)

        def _on_invalidate(*_args: Any) -> None:
            invalidated.inc()

        event.listen(sync_engine, "invalidate", _on_invalidate)
        event.listen(sync_engine, "soft_invalidate", _on_invalidate)


def _pool_stat(sync_engine: Any, name: str) -> int:
    """One statistic of the engine's current pool (it changes on ``dispose()``); 0 when not tracked."""
    reader = getattr(sync_engine.pool, name, None)
    if not callable(reader):
        return 0
    value = reader()
    return int(value) if isinstance(value, int) else 0
