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
"""``@async_method`` dispatch: the call returns at once, the method runs in a task of its own (Spring ``@Async``).

The application context replaces every ``@async_method`` of a bean with :func:`dispatching` over it:

- **The caller returns at once.** ``await service.audit(order)`` submits the call through the
  :class:`~pyfly.scheduling.ports.outbound.TaskExecutorPort` and gives back the :class:`asyncio.Task` running
  it; await that task for the method's result (``result = await (await service.audit(order))``).
- **Its own unit of work.** The task starts with the transaction state cleared (the executors use
  :func:`pyfly.data.transaction.detached`): a ``@transactional`` method opens its own transaction instead of
  joining the caller's, so neither one's failure rolls the other back, and the caller's commit never waits
  for it.
- **Uncaught exceptions** go to the :class:`AsyncUncaughtExceptionHandler` bean (Spring's
  ``AsyncUncaughtExceptionHandler``), by default :class:`LoggingAsyncUncaughtExceptionHandler`, which logs them
  at ``ERROR``. A caller that awaits the task gets the exception too.
- **Synchronous methods** run in the executor's thread pool (``ThreadPoolTaskExecutor``), or through
  ``asyncio.to_thread``.

The calls are drained when the application context stops: the executor belongs to the ``TaskScheduler``,
which the context stops (waiting for its tasks) before any ``@pre_destroy``.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, runtime_checkable

from pyfly.scheduling.ports.outbound import TaskExecutorPort

logger = logging.getLogger(__name__)


@runtime_checkable
class AsyncUncaughtExceptionHandler(Protocol):
    """Receives the exceptions ``@async_method`` calls raise (Spring's ``AsyncUncaughtExceptionHandler``).

    Declare a bean implementing it to replace the default, which logs them.
    """

    def handle_uncaught_exception(
        self, error: BaseException, method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        """Handle *error*, raised by ``method(*args, **kwargs)`` in its own task."""
        ...


class LoggingAsyncUncaughtExceptionHandler:
    """The default handler: logs the exception at ``ERROR`` with the method's name."""

    def handle_uncaught_exception(
        self, error: BaseException, method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        logger.error(
            "async_method_failed",
            extra={"method": getattr(method, "__qualname__", repr(method))},
            exc_info=(type(error), error, error.__traceback__),
        )


def dispatching(
    method: Callable[..., Any],
    executor: TaskExecutorPort,
    handler: AsyncUncaughtExceptionHandler | None = None,
) -> Callable[..., Coroutine[Any, Any, asyncio.Task[Any]]]:
    """Wrap the bound *method* so that a call submits it through *executor* and returns its task at once."""
    report = handler or LoggingAsyncUncaughtExceptionHandler()

    @functools.wraps(method)
    async def dispatch(*args: Any, **kwargs: Any) -> asyncio.Task[Any]:
        if inspect.iscoroutinefunction(method):
            work: Coroutine[Any, Any, Any] = method(*args, **kwargs)
        else:
            work = _run_sync(executor, functools.partial(method, *args, **kwargs))
        task = await executor.submit(work)
        task.add_done_callback(functools.partial(_report, report, method, args, kwargs))
        return task

    return dispatch


async def _run_sync(executor: TaskExecutorPort, call: Callable[[], Any]) -> Any:
    run_sync = getattr(executor, "run_sync", None)
    return await (run_sync(call) if callable(run_sync) else asyncio.to_thread(call))


def _report(
    handler: AsyncUncaughtExceptionHandler,
    method: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    task: asyncio.Task[Any],
) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    try:
        handler.handle_uncaught_exception(error, method, args, kwargs)
    except Exception:  # noqa: BLE001 — a failing handler must not hide the method's failure
        logger.exception("async_method_exception_handler_failed")
