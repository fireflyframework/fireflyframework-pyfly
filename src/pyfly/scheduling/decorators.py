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
"""Decorators for scheduled task execution and async method offloading."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any


def scheduled(
    *,
    cron: str | None = None,
    fixed_rate: timedelta | None = None,
    fixed_delay: timedelta | None = None,
    initial_delay: timedelta | None = None,
    zone: str | None = None,
    lock: str | bool | None = None,
    lock_ttl: timedelta | None = None,
    concurrent: int = 1,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a method to be scheduled for periodic execution.

    Exactly one of cron, fixed_rate, or fixed_delay must be provided. A job never runs concurrently with
    itself unless *concurrent* says so (Spring's scheduler contract).

    - cron: 5- or 6-field cron expression (e.g., "0 0 * * *" for midnight); the next fire time is computed
      once the previous run has ended (fires missed meanwhile are skipped)
    - fixed_rate: Start a run every interval; a run still going when the next is due delays it (the next run
      starts late, never alongside it)
    - fixed_delay: Wait delay after previous run completes
    - initial_delay: Optional delay before first execution
    - zone: IANA time zone for ``cron`` evaluation (e.g. "America/New_York");
      defaults to UTC. Spring's ``@Scheduled(zone=...)``.
    - lock: distributed lock so only one instance per cluster runs a tick
      (ShedLock / Spring ``@SchedulerLock``). ``True`` auto-derives the name
      ``"Class.method"``; a string sets an explicit shared name; ``None`` disables.
      Requires a ``DistributedLock`` bean for cross-process coordination.
    - lock_ttl: max time the lock is held before auto-expiry (default 60s). A run still going at the ttl is
      cancelled: the lock has ended, and another instance may start the job.
    - concurrent: how many runs of the job may be in flight at once (``fixed_rate`` and ``cron``; default 1).
      Each run of a ``@transactional`` job holds a pooled connection, so keep it well below the pool size.
    """
    triggers = sum(x is not None for x in (cron, fixed_rate, fixed_delay))
    if triggers != 1:
        raise ValueError("Exactly one of cron, fixed_rate, or fixed_delay must be specified")
    if concurrent < 1:
        raise ValueError(f"concurrent must be at least 1, got {concurrent}")
    if fixed_delay is not None and concurrent != 1:
        raise ValueError("fixed_delay starts a run after the previous one ended: concurrent must be 1")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.__pyfly_scheduled__ = True  # type: ignore[attr-defined]
        func.__pyfly_scheduled_cron__ = cron  # type: ignore[attr-defined]
        func.__pyfly_scheduled_fixed_rate__ = fixed_rate  # type: ignore[attr-defined]
        func.__pyfly_scheduled_fixed_delay__ = fixed_delay  # type: ignore[attr-defined]
        func.__pyfly_scheduled_initial_delay__ = initial_delay  # type: ignore[attr-defined]
        func.__pyfly_scheduled_zone__ = zone  # type: ignore[attr-defined]
        func.__pyfly_scheduled_lock__ = lock  # type: ignore[attr-defined]
        func.__pyfly_scheduled_lock_ttl__ = lock_ttl.total_seconds() if lock_ttl else None  # type: ignore[attr-defined]
        func.__pyfly_scheduled_concurrent__ = concurrent  # type: ignore[attr-defined]
        return func

    return decorator


def async_method(func: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a method to execute asynchronously via TaskExecutor.

    The caller returns immediately -- the actual execution is offloaded.
    """
    func.__pyfly_async__ = True  # type: ignore[attr-defined]
    return func
