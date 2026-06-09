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
"""SQLAlchemy query-metrics adapter — R2dbcMetrics parity.

Attaches SQLAlchemy core event listeners to the engine's ``sync_engine`` to
record per-operation query duration (histogram), query count (counter), and
query errors (counter) via a :class:`~pyfly.observability.ports.MetricsRecorder`.

The ``operation`` label is restricted to ``{SELECT, INSERT, UPDATE, DELETE, OTHER}``
so Prometheus cardinality stays bounded regardless of query shape.
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
