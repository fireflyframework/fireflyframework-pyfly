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
"""Observability adapters for transactional lifecycle events.

This module provides two ``TransactionalEventsPort`` implementations:

* :class:`LoggerEventsAdapter` -- writes structured log messages for every
  lifecycle event emitted by the transactional engine.
* :class:`CompositeEventsAdapter` -- fans-out each event to an ordered
  sequence of child adapters, absorbing individual adapter failures so that
  one broken sink never silences the others.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pyfly.transactional.shared.ports.outbound import TransactionalEventsPort

_logger = logging.getLogger("pyfly.transactional.events")


# ---------------------------------------------------------------------------
# LoggerEventsAdapter
# ---------------------------------------------------------------------------


class LoggerEventsAdapter:
    """Logs transactional lifecycle events via the standard ``logging`` module.

    All messages are emitted through the
    ``pyfly.transactional.events`` logger.  Successful operations log at
    :data:`logging.INFO`; failures and compensation errors log at
    :data:`logging.WARNING`.
    """

    async def on_start(self, name: str, correlation_id: str) -> None:
        _logger.info("Saga '%s' started [correlation_id=%s]", name, correlation_id)

    async def on_step_success(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        attempts: int,
        latency_ms: float,
    ) -> None:
        _logger.info(
            "Step '%s' succeeded [saga=%s, attempts=%d, latency=%.1fms]",
            step_id,
            name,
            attempts,
            latency_ms,
        )

    async def on_step_failed(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        error: Exception,
        attempts: int,
        latency_ms: float,
    ) -> None:
        _logger.warning(
            "Step '%s' failed [saga=%s, attempts=%d, latency=%.1fms]: %s",
            step_id,
            name,
            attempts,
            latency_ms,
            error,
        )

    async def on_compensated(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        error: Exception | None,
    ) -> None:
        if error is None:
            _logger.info("Step '%s' compensated [saga=%s]", step_id, name)
        else:
            _logger.warning("Step '%s' compensated [saga=%s]: %s", step_id, name, error)

    async def on_completed(self, name: str, correlation_id: str, success: bool) -> None:
        _logger.info(
            "Saga '%s' completed [correlation_id=%s, success=%s]",
            name,
            correlation_id,
            success,
        )


# ---------------------------------------------------------------------------
# CompositeEventsAdapter
# ---------------------------------------------------------------------------


class CompositeEventsAdapter:
    """Broadcasts transactional events to multiple ``TransactionalEventsPort`` adapters.

    If an individual adapter raises an exception, the error is logged and
    the remaining adapters still receive the event.

    Args:
        *adapters: One or more :class:`TransactionalEventsPort` implementations
            to broadcast events to.
    """

    def __init__(self, *adapters: TransactionalEventsPort) -> None:
        self._adapters: Sequence[TransactionalEventsPort] = adapters

    # -- internal broadcast helper ------------------------------------------

    async def _broadcast(self, method: str, *args: object, **kwargs: object) -> None:
        for adapter in self._adapters:
            try:
                await getattr(adapter, method)(*args, **kwargs)
            except Exception:
                _logger.error(
                    "Events adapter %r failed on %s",
                    adapter,
                    method,
                    exc_info=True,
                )

    # -- TransactionalEventsPort interface ----------------------------------

    async def on_start(self, name: str, correlation_id: str) -> None:
        await self._broadcast("on_start", name, correlation_id)

    async def on_step_success(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        attempts: int,
        latency_ms: float,
    ) -> None:
        await self._broadcast(
            "on_step_success",
            name,
            correlation_id,
            step_id,
            attempts=attempts,
            latency_ms=latency_ms,
        )

    async def on_step_failed(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        error: Exception,
        attempts: int,
        latency_ms: float,
    ) -> None:
        await self._broadcast(
            "on_step_failed",
            name,
            correlation_id,
            step_id,
            error=error,
            attempts=attempts,
            latency_ms=latency_ms,
        )

    async def on_compensated(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        error: Exception | None,
    ) -> None:
        await self._broadcast("on_compensated", name, correlation_id, step_id, error=error)

    async def on_completed(self, name: str, correlation_id: str, success: bool) -> None:
        await self._broadcast("on_completed", name, correlation_id, success=success)
