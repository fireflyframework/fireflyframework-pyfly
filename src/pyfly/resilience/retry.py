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
"""@retry — declarative retry with backoff (Spring Retry / Resilience4j @Retry equivalent).

Retries and transactions
------------------------

A retry must run *outside* the unit of work it retries: every attempt then gets a fresh transaction, and a
failed attempt's writes roll back with it. In Spring the order of ``@Retryable`` and ``@Transactional`` on a
method does not matter, because the retry advice always runs outside the transaction interceptor. PyFly
keeps that rule whichever order the decorators are written in: ``@transactional`` applied over ``@retry``
moves the retry outside the transaction.

Three more rules keep a retry from doing harm:

- An exception whose class declares ``retryable = False`` is never retried. The unit of work's
  :class:`~pyfly.data.transaction.errors.CommitOutcomeUnknownError` does: a commit interrupted in flight
  may have committed, and retrying it would apply the writes twice.
- A retry that runs *inside* a unit of work (a retried method called from a transactional caller) stops
  as soon as that unit is rollback-only: no later attempt can commit it.
- Retry only transient failures: list them in ``exceptions`` (a connection error, a serialization
  failure, a deadlock), not ``Exception``, when the call writes.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import random
import time
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger(__name__)


def _retryable(error: BaseException) -> bool:
    """Whether *error* may be retried: its class does not declare ``retryable = False``, and it did not
    leave a unit of work the retry runs inside rollback-only."""
    if getattr(error, "retryable", True) is False:
        return False
    try:
        from pyfly.data.transaction.context import current_state
    except ImportError:  # pragma: no cover — the data package ships with the framework
        return True
    for _datasource, bound in current_state().units:
        if getattr(bound, "rollback_only", False):
            _logger.warning(
                "retry_stopped_inside_rollback_only_unit",
                extra={"error": type(error).__name__, "datasource": _datasource},
            )
            return False
    return True


def retry(
    max_attempts: int = 3,
    *,
    delay: float = 0.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
    jitter: float = 0.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Re-invoke the wrapped callable up to *max_attempts* times while it raises one of
    *exceptions*, sleeping ``delay * backoff ** attempt`` (capped at *max_delay*) between
    attempts. The last exception is re-raised once attempts are exhausted. Works on both
    sync and async callables.

    Args:
        max_attempts: Total attempts including the first (>= 1).
        delay: Base delay (seconds) before the first retry.
        backoff: Multiplier applied to the delay each subsequent attempt.
        max_delay: Optional cap on the per-attempt delay.
        jitter: Randomization fraction in ``[0, 1]`` applied to each wait
            (``±jitter * wait``) to avoid thundering-herd retries.
        exceptions: Exception types that trigger a retry; others propagate immediately. An exception
            whose class declares ``retryable = False`` (``CommitOutcomeUnknownError``) never does.

    Put it outside ``@transactional`` (see the module documentation); written the other way round,
    ``@transactional`` moves it outside for you.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def _wait(attempt: int) -> float:
        computed = delay * (backoff**attempt)
        if jitter:
            computed += random.uniform(-jitter, jitter) * computed
        capped = min(computed, max_delay) if max_delay is not None else computed
        return max(0.0, capped)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                last: BaseException | None = None
                for attempt in range(max_attempts):
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as exc:
                        last = exc
                        if attempt + 1 >= max_attempts or not _retryable(exc):
                            break
                        wait = _wait(attempt)
                        if wait > 0:
                            await asyncio.sleep(wait)
                assert last is not None  # noqa: S101 - loop always sets last before break
                raise last

            # @transactional applied over this wrapper re-applies the retry outside the transaction.
            async_wrapper.__pyfly_retry__ = decorator  # type: ignore[attr-defined]
            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            last: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last = exc
                    if attempt + 1 >= max_attempts or not _retryable(exc):
                        break
                    wait = _wait(attempt)
                    if wait > 0:
                        time.sleep(wait)
            assert last is not None  # noqa: S101 - loop always sets last before break
            raise last

        return sync_wrapper

    return decorator
