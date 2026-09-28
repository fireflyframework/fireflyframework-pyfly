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
"""Health and metrics of the document datasource (C166).

:class:`MongoHealthIndicator` answers the readiness probe (and only it, as the relational database indicator
does: a MongoDB blip takes the pod out of the load balancer instead of restarting every replica): the server's
answer to ``ping`` within ``pyfly.data.document.health.timeout`` (2 s by default), whatever the client's server
selection timeout is. Its details name the datasource, the database, and whether the server runs transactions
(a replica set or a sharded cluster).

:class:`MongoMetrics` is a pymongo command and connection-pool listener that feeds the metrics registry, the
driver-level counterpart of the relational pool and query metrics (labeled ``datasource``):

============================================  ===========================================================
Metric                                        Meaning
============================================  ===========================================================
``pyfly_mongo_commands_total``                Commands sent, by ``command`` (a bounded set of names) and
                                              ``outcome`` (``success``/``failure``)
``pyfly_mongo_command_duration_seconds``      Histogram: how long each command took, by ``command``
``pyfly_mongo_pool_checked_out``              Connections in use
``pyfly_mongo_pool_open``                     Connections open (idle or in use)
``pyfly_mongo_pool_acquire_seconds``          Histogram: how long a checkout waited for a connection
``pyfly_mongo_pool_checkout_failures_total``  Checkouts that failed (a pool timeout, a closed pool...)
============================================  ===========================================================
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pymongo import monitoring

from pyfly.actuator.health import HealthStatus, ProbeGroup

_COMMANDS = frozenset(
    {
        "find",
        "insert",
        "update",
        "delete",
        "aggregate",
        "count",
        "distinct",
        "getMore",
        "findAndModify",
        "commitTransaction",
        "abortTransaction",
        "bulkWrite",
    }
)
"""The command names a metric keeps; any other is ``other`` (bounded cardinality)."""

_DURATION_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_ACQUIRE_BUCKETS = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0)


class MongoHealthIndicator:
    """Readiness check of the document datasource: ``ping`` within *timeout* seconds (module documentation)."""

    probe_groups = frozenset({ProbeGroup.READINESS})

    def __init__(
        self,
        client: Any,
        *,
        datasource: str = "document",
        database: str | None = None,
        timeout: float = 2.0,
        manager: Any = None,
    ) -> None:
        self._client = client
        self._datasource = datasource
        self._database = database
        self._timeout = timeout
        self._manager = manager

    async def health(self) -> HealthStatus:
        details: dict[str, Any] = {"database": "mongodb", "datasource": self._datasource}
        if self._database is not None:
            details["name"] = self._database
        try:
            async with asyncio.timeout(self._timeout):
                await self._client.admin.command("ping")
                if self._manager is not None:
                    details["transactions"] = await self._manager.supports_transactions()
        except TimeoutError:
            return HealthStatus(status="DOWN", details={**details, "error": f"no answer within {self._timeout:g} s"})
        except Exception as error:  # noqa: BLE001 — any failure to reach the server is DOWN
            return HealthStatus(status="DOWN", details={**details, "error": type(error).__name__})
        return HealthStatus(status="UP", details=details)


class MongoMetrics(monitoring.CommandListener, monitoring.ConnectionPoolListener):
    """pymongo listener that records command and pool metrics (module documentation).

    *recorder* returns the application's metrics recorder (``None``: nothing is recorded), asked at the first
    event: the client is built before the metrics beans may exist.
    """

    def __init__(self, recorder: Callable[[], Any], *, datasource: str = "document") -> None:
        self._recorder = recorder
        self._datasource = datasource
        self._metrics: dict[str, Any] | None = None
        self._unavailable = False

    def _handles(self) -> dict[str, Any] | None:
        if self._metrics is not None or self._unavailable:
            return self._metrics
        recorder = self._recorder()
        if recorder is None:
            self._unavailable = True  # asked once the context runs: no metrics recorder is configured
            return None
        self._metrics = {
            "commands": recorder.counter(
                "pyfly_mongo_commands_total", "MongoDB commands sent", ["datasource", "command", "outcome"]
            ),
            "duration": recorder.histogram(
                "pyfly_mongo_command_duration_seconds",
                "MongoDB command duration",
                ["datasource", "command"],
                buckets=_DURATION_BUCKETS,
            ),
            "checked_out": recorder.gauge("pyfly_mongo_pool_checked_out", "MongoDB connections in use", ["datasource"]),
            "open": recorder.gauge("pyfly_mongo_pool_open", "MongoDB connections open", ["datasource"]),
            "acquire": recorder.histogram(
                "pyfly_mongo_pool_acquire_seconds",
                "Time a MongoDB checkout waited for a connection",
                ["datasource"],
                buckets=_ACQUIRE_BUCKETS,
            ),
            "checkout_failures": recorder.counter(
                "pyfly_mongo_pool_checkout_failures_total", "MongoDB checkouts that failed", ["datasource"]
            ),
        }
        return self._metrics

    def _command(self, event: Any, outcome: str | None) -> None:
        handles = self._handles()
        if handles is None:
            return
        name = event.command_name if event.command_name in _COMMANDS else "other"
        if outcome is not None:
            handles["commands"].labels(datasource=self._datasource, command=name, outcome=outcome).inc()
            handles["duration"].labels(datasource=self._datasource, command=name).observe(
                event.duration_micros / 1_000_000
            )

    # -- commands ------------------------------------------------------------------------------------------

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        """Nothing: a command is counted when it ends."""

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        self._command(event, "success")

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        self._command(event, "failure")

    # -- the pool --------------------------------------------------------------------------------------------

    def _gauge(self, name: str, delta: int) -> None:
        handles = self._handles()
        if handles is not None:
            handles[name].labels(datasource=self._datasource).inc(delta)

    def connection_created(self, event: monitoring.ConnectionCreatedEvent) -> None:
        self._gauge("open", 1)

    def connection_closed(self, event: monitoring.ConnectionClosedEvent) -> None:
        self._gauge("open", -1)

    def connection_checked_out(self, event: monitoring.ConnectionCheckedOutEvent) -> None:
        self._gauge("checked_out", 1)
        handles = self._handles()
        duration = getattr(event, "duration", None)
        if handles is not None and isinstance(duration, (int, float)):
            handles["acquire"].labels(datasource=self._datasource).observe(duration)

    def connection_checked_in(self, event: monitoring.ConnectionCheckedInEvent) -> None:
        self._gauge("checked_out", -1)

    def connection_check_out_failed(self, event: monitoring.ConnectionCheckOutFailedEvent) -> None:
        handles = self._handles()
        if handles is not None:
            handles["checkout_failures"].labels(datasource=self._datasource).inc()

    def pool_created(self, event: monitoring.PoolCreatedEvent) -> None:
        """Nothing to record."""

    def pool_ready(self, event: monitoring.PoolReadyEvent) -> None:
        """Nothing to record."""

    def pool_cleared(self, event: monitoring.PoolClearedEvent) -> None:
        """Nothing to record: the connections closed report themselves."""

    def pool_closed(self, event: monitoring.PoolClosedEvent) -> None:
        """Nothing to record: the connections closed report themselves."""

    def connection_ready(self, event: monitoring.ConnectionReadyEvent) -> None:
        """Nothing to record."""

    def connection_check_out_started(self, event: monitoring.ConnectionCheckOutStartedEvent) -> None:
        """Nothing to record."""
