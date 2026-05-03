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
"""Saga recovery service — recover interrupted sagas from persistence.

This module provides :class:`SagaRecoveryService`, which loads stale
in-flight saga executions from the persistence layer and marks them as
failed so the system can move forward.  An optional events port is used
to emit warning-level lifecycle events for each recovered saga.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pyfly.transactional.shared.ports.outbound import (
    TransactionalEventsPort,
    TransactionalPersistencePort,
)

logger = logging.getLogger(__name__)


class SagaRecoveryService:
    """Recovers interrupted saga executions by loading stale in-flight sagas and compensating.

    The recovery service is designed to run periodically (e.g. via a
    scheduler) to detect sagas that have been stuck in an ``IN_FLIGHT``
    state longer than a configurable threshold.  Such sagas are marked as
    ``FAILED`` in persistence and a lifecycle event is emitted when an
    events port is available.

    Parameters
    ----------
    persistence_port:
        Adapter satisfying :class:`TransactionalPersistencePort` used to
        query and update saga execution state.
    saga_engine:
        Optional reference to the :class:`SagaEngine`.  Accepted as
        ``Any`` to avoid circular import issues.  Reserved for future use
        when active compensation replay is implemented.
    events_port:
        Optional adapter satisfying :class:`TransactionalEventsPort` used
        to emit lifecycle events for recovered sagas.
    """

    def __init__(
        self,
        persistence_port: TransactionalPersistencePort,
        saga_engine: Any | None = None,
        events_port: TransactionalEventsPort | None = None,
    ) -> None:
        self._persistence_port = persistence_port
        self._saga_engine = saga_engine
        self._events_port = events_port

    # ── public API ────────────────────────────────────────────

    async def recover_stale(self, stale_threshold_seconds: int = 600) -> int:
        """Find and compensate sagas that have been in-flight too long.

        The algorithm:

        1. Calculate a UTC cutoff timestamp from the current time minus
           *stale_threshold_seconds*.
        2. Query persistence for all sagas whose last update is older than
           the cutoff via :pymethod:`get_stale`.
        3. For each stale saga that is still ``IN_FLIGHT``:
           a. Mark the saga as ``FAILED`` via :pymethod:`mark_completed`.
           b. Emit a warning lifecycle event if an events port is
              configured.
           c. Increment the recovery counter.
        4. Return the total count of recovered sagas.

        Args:
            stale_threshold_seconds: Consider sagas stale after this many
                seconds.  Defaults to ``600`` (10 minutes).

        Returns:
            Number of sagas recovered/compensated.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_threshold_seconds)
        stale_sagas = await self._persistence_port.get_stale(cutoff)

        recovered = 0
        for saga_state in stale_sagas:
            # Only recover sagas that are still in-flight
            if saga_state.get("status") != "IN_FLIGHT":
                continue

            correlation_id: str = saga_state["correlation_id"]
            saga_name: str = saga_state.get("saga_name", "unknown")

            logger.warning(
                "Recovering stale saga %r (correlation_id=%s)",
                saga_name,
                correlation_id,
            )

            await self._persistence_port.mark_completed(correlation_id, successful=False)

            if self._events_port is not None:
                await self._events_port.on_completed(saga_name, correlation_id, success=False)

            recovered += 1

        return recovered

    async def cleanup(self, older_than_hours: int = 24) -> int:
        """Clean up old completed saga states from persistence.

        Delegates to the persistence port's
        :pymethod:`~TransactionalPersistencePort.cleanup` method with the
        given age threshold.

        Args:
            older_than_hours: Remove completed sagas older than this many
                hours.  Defaults to ``24``.

        Returns:
            Number of states cleaned up.
        """
        return await self._persistence_port.cleanup(timedelta(hours=older_than_hours))
