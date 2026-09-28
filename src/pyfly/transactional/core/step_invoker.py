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
"""Generic step / participant invoker — retry, jitter, timeout, cpu-bound dispatch.

Each attempt runs inside :func:`pyfly.data.transaction.track_commits`. An attempt that failed, timed out or
was cancelled after a unit of work of it committed (or with a commit whose outcome is unknown) is recorded on
the context (:meth:`~pyfly.transactional.core.context.ExecutionContext.note_step_committed`), so the step is
compensated like a completed one, and it is never retried: a retry would apply its writes twice. The
timeout bounds each attempt; one that fires while the attempt's ``COMMIT`` is in flight lets the commit finish
(commits are shielded) and then fails the attempt.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
import typing
from collections.abc import Callable
from typing import Any

from pyfly.data.transaction import track_commits
from pyfly.transactional.core.argument import (
    ArgumentResolver,
    SetVariable,
    extract_marker,
)
from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.exceptions import StepFailedError, StepTimeoutError
from pyfly.transactional.core.model import RetryPolicy

_logger = logging.getLogger(__name__)


class StepInvoker:
    """Calls step methods with retry, exponential backoff, jitter and timeout."""

    def __init__(self, argument_resolver: ArgumentResolver | None = None) -> None:
        self._resolver = argument_resolver or ArgumentResolver()

    async def invoke(
        self,
        *,
        bean: Any,
        method: Callable[..., Any],
        step_id: str,
        ctx: ExecutionContext,
        retry_policy: RetryPolicy,
        cpu_bound: bool = False,
        compensation_error: BaseException | None = None,
        compensation_results: dict[str, Any] | None = None,
        current_participant_id: str | None = None,
    ) -> Any:
        """Resolve arguments and invoke *method* under the configured policy.

        Returns the value the method produced.

        Raises:
            StepFailedError:  Method exhausted all retries.
            StepTimeoutError: Method exceeded ``timeout_ms`` on the last attempt.
        """
        last_error: BaseException | None = None
        attempts = 0
        max_attempts = max(1, retry_policy.max_attempts)

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            await ctx.record_step_started(step_id)
            started = time.perf_counter()
            with track_commits() as commits:
                try:
                    kwargs = self._resolver.resolve(
                        method,
                        ctx,
                        compensation_error=compensation_error,
                        compensation_results=compensation_results,
                        current_participant_id=current_participant_id,
                        skip_first=bean is not None,
                    )
                    value = await self._call(method, bean, kwargs, retry_policy.timeout_ms, cpu_bound, step_id)
                except StepTimeoutError as exc:
                    last_error = exc
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    await ctx.record_step_failure(step_id, exc, latency_ms)
                    _logger.warning("step %s timed out (attempt %d/%d)", step_id, attempt, max_attempts)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    await ctx.record_step_failure(step_id, exc, latency_ms)
                    _logger.warning("step %s failed (attempt %d/%d): %s", step_id, attempt, max_attempts, exc)
                except BaseException:
                    # Cancelled (a sibling failed, the workflow timed out or was cancelled): the step ends here,
                    # and what it committed first is compensated.
                    ctx.note_step_cancelled(step_id, committed=commits.may_have_committed)
                    raise
                else:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    await ctx.record_step_success(step_id, value, latency_ms)
                    await self._apply_set_variables(method, kwargs, ctx)
                    return value
            if commits.may_have_committed:
                # Work of this attempt committed: never retry it, compensate it like a completed step.
                ctx.note_step_committed(step_id)
                _logger.warning("step %s committed before it failed; not retried", step_id)
                break
            if attempt < max_attempts:
                delay = self._compute_backoff(retry_policy, attempt)
                if delay > 0:
                    await asyncio.sleep(delay)

        assert last_error is not None
        raise StepFailedError(step_id, attempts, last_error) from last_error

    # -- internals ----------------------------------------------------------

    @staticmethod
    async def _call(
        method: Callable[..., Any],
        bean: Any,
        kwargs: dict[str, Any],
        timeout_ms: int,
        cpu_bound: bool,
        step_id: str,
    ) -> Any:
        async def runner() -> Any:
            args: tuple[Any, ...] = (bean,) if bean is not None else ()
            if cpu_bound:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, lambda: method(*args, **kwargs))
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        if timeout_ms > 0:
            try:
                async with asyncio.timeout(timeout_ms / 1000.0):
                    return await runner()
            except TimeoutError as exc:
                raise StepTimeoutError(step_id=step_id, timeout_ms=timeout_ms) from exc
        return await runner()

    @staticmethod
    def _compute_backoff(policy: RetryPolicy, attempt: int) -> float:
        if policy.backoff_ms <= 0:
            return 0.0
        base_ms: float = float(policy.backoff_ms * (2 ** (attempt - 1)))
        if policy.jitter and policy.jitter_factor > 0:
            jitter_range = base_ms * policy.jitter_factor
            base_ms += random.uniform(0, jitter_range)
        return base_ms / 1000.0

    @staticmethod
    async def _apply_set_variables(method: Callable[..., Any], kwargs: dict[str, Any], ctx: ExecutionContext) -> None:
        try:
            type_hints = typing.get_type_hints(method, include_extras=True)
        except Exception:
            type_hints = {}
        for name, hint in type_hints.items():
            marker, _ = extract_marker(hint)
            if isinstance(marker, SetVariable):
                container = kwargs.get(name)
                if isinstance(container, dict):
                    for k, v in container.items():
                        await ctx.set_variable(k, v)
                elif marker.name is not None and container is not None:
                    await ctx.set_variable(marker.name, container)
