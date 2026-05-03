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
"""CQRS metrics collection for commands and queries.

Mirrors Java's ``CommandMetricsService``.  If no metrics registry is
provided, all recording methods are silent no-ops.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_logger = logging.getLogger(__name__)


class CqrsMetricsService:
    """Records metrics for CQRS command/query processing.

    Metric names follow the Java counterpart:

    * ``firefly.cqrs.command.processed`` — success counter
    * ``firefly.cqrs.command.failed`` — failure counter
    * ``firefly.cqrs.command.validation.failed`` — validation failure counter
    * ``firefly.cqrs.command.processing.time`` — processing duration (seconds)
    * ``firefly.cqrs.query.processed`` — success counter
    * ``firefly.cqrs.query.processing.time`` — processing duration (seconds)
    """

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry
        if registry:
            self._cmd_processed = registry.counter("firefly_cqrs_command_processed", "Successful commands")
            self._cmd_failed = registry.counter("firefly_cqrs_command_failed", "Failed commands")
            self._cmd_validation_failed = registry.counter(
                "firefly_cqrs_command_validation_failed", "Command validation failures"
            )
            self._cmd_time = registry.histogram(
                "firefly_cqrs_command_processing_time_seconds", "Command processing duration"
            )
            self._qry_processed = registry.counter("firefly_cqrs_query_processed", "Successful queries")
            self._qry_failed = registry.counter("firefly_cqrs_query_failed", "Failed queries")
            self._qry_time = registry.histogram(
                "firefly_cqrs_query_processing_time_seconds", "Query processing duration"
            )
        else:
            self._cmd_processed = None
            self._cmd_failed = None
            self._cmd_validation_failed = None
            self._cmd_time = None
            self._qry_processed = None
            self._qry_failed = None
            self._qry_time = None

    # ── command metrics ────────────────────────────────────────

    def record_command_success(self, command: Any, duration_s: float) -> None:
        if self._cmd_processed:
            self._cmd_processed.inc()
        if self._cmd_time:
            self._cmd_time.observe(duration_s)
        _logger.debug(
            "Command %s succeeded in %.3fs",
            type(command).__name__,
            duration_s,
        )

    def record_command_failure(self, command: Any, error: Exception, duration_s: float) -> None:
        if self._cmd_failed:
            self._cmd_failed.inc()
        if self._cmd_time:
            self._cmd_time.observe(duration_s)
        _logger.debug(
            "Command %s failed in %.3fs: %s",
            type(command).__name__,
            duration_s,
            error,
        )

    def record_validation_failure(self, command: Any) -> None:
        if self._cmd_validation_failed:
            self._cmd_validation_failed.inc()

    # ── query metrics ──────────────────────────────────────────

    def record_query_success(self, query: Any, duration_s: float) -> None:
        if self._qry_processed:
            self._qry_processed.inc()
        if self._qry_time:
            self._qry_time.observe(duration_s)

    def record_query_failure(self, query: Any, error: Exception, duration_s: float) -> None:
        if self._qry_failed:
            self._qry_failed.inc()
        if self._qry_time:
            self._qry_time.observe(duration_s)

    # ── utility ────────────────────────────────────────────────

    @staticmethod
    def now() -> float:
        """High-resolution monotonic timestamp for duration calculation."""
        return time.monotonic()

    # ── introspection ──────────────────────────────────────────

    @property
    def has_registry(self) -> bool:
        return self._registry is not None
