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
"""The Firefly OpenFeature hooks (spec 4.9), attached to the framework's client only.

- :class:`MetricsHook` counts every evaluation in ``feature_flag_evaluations_total{flag, variant, reason}``.
- :class:`ExposureEventHook` publishes :class:`~pyfly.feature_flags.events.FeatureFlagEvaluated` (exposure records
  for experiments); the auto-configuration attaches it only when ``pyfly.feature-flags.events.evaluations`` is on.

Both run in ``finally_after`` (which the SDK calls after every evaluation, failed ones included), skip the
management preview (hint ``pyfly.preview``), and never raise: a telemetry failure is logged at DEBUG and the
evaluation is unaffected. OpenFeature hooks are synchronous while PyFly's publisher is async, so the exposure hook
schedules the publish on the running loop, hands it to the application's loop from a worker thread, or, with no
loop at all (a CLI script), publishes through a short-lived one. Scheduled publishes are kept until they settle, so
none is garbage-collected mid-flight, and :meth:`ExposureEventHook.drain` awaits them on shutdown.

Because the event is published later, an object value (a ``dict`` or a ``list``) is copied into it: the caller owns
the value it was served and may adapt it, and the exposure record must still say what was served (the provider gives
every evaluation a copy of its own, which the caller and the event would otherwise share). A preflight counts at
most 10,000 value occurrences (containers and scalars, repeated references counted again); oversized values omit
the exposure with best-effort DEBUG logging, leaving evaluation and metrics unchanged.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from openfeature.flag_evaluation import FlagEvaluationDetails, FlagValueType, Reason
from openfeature.hook import Hook, HookContext, HookHints

from pyfly.feature_flags.client import PREVIEW_HINT
from pyfly.feature_flags.events import FeatureFlagEvaluated

if TYPE_CHECKING:
    from pyfly.context.events import ApplicationEventPublisher
    from pyfly.observability.ports import MetricsRecorder

__all__ = ["EVALUATIONS_METRIC", "ExposureEventHook", "MetricsHook", "MetricsSource"]

_logger = logging.getLogger(__name__)

EVALUATIONS_METRIC = "feature_flag_evaluations_total"

MetricsSource = Callable[[], "MetricsRecorder | None"]
"""A function returning the metrics recorder, or ``None`` while there is none."""


def _failed(details: FlagEvaluationDetails[Any]) -> bool:
    return details.error_code is not None or details.reason == Reason.ERROR


def _reason(details: FlagEvaluationDetails[Any]) -> str:
    if _failed(details):
        return "ERROR"
    return str(details.reason) if details.reason is not None else "UNKNOWN"


def _variant(details: FlagEvaluationDetails[Any]) -> str:
    return details.variant if not _failed(details) and details.variant else "none"


class MetricsHook(Hook):
    """Counts evaluations by flag, variant (``none`` without one) and reason (``ERROR`` for a failure).

    *metrics* is a recorder, or a function returning one (``None`` while there is none): the auto-configuration
    passes a lookup because the ``MetricsRegistry`` bean comes from an auto-configuration processed after this
    one (entry points are processed by name: ``feature-flags`` before ``metrics``). The counter is created by the
    first evaluation that has a recorder, so building the hook cannot fail and an unavailable registry is retried.
    """

    def __init__(self, metrics: MetricsRecorder | MetricsSource | None) -> None:
        self._recorder: MetricsRecorder | None = None
        self._source: MetricsSource | None = None
        self._counter: Any = None
        self._creating = threading.Lock()  # the registry's counter() is check-then-create: one thread creates
        if metrics is None:
            return
        if hasattr(metrics, "counter"):
            self._recorder = cast("MetricsRecorder", metrics)
        else:
            self._source = metrics

    def _current(self) -> Any:
        counter = self._counter
        if counter is not None:
            return counter
        recorder = self._recorder
        if recorder is None and self._source is not None:
            recorder = self._recorder = self._source()  # asked at every evaluation until it answers, then never again
        if recorder is None:
            return None
        with self._creating:
            if self._counter is None:  # another thread may have created it while this one waited
                self._counter = recorder.counter(
                    EVALUATIONS_METRIC,
                    "Feature flag evaluations by flag, variant and reason",
                    ["flag", "variant", "reason"],
                )
            return self._counter

    def finally_after(
        self, hook_context: HookContext, details: FlagEvaluationDetails[FlagValueType], hints: HookHints
    ) -> None:
        if hints.get(PREVIEW_HINT):
            return
        try:
            counter = self._current()
            if counter is not None:
                counter.labels(flag=hook_context.flag_key, variant=_variant(details), reason=_reason(details)).inc()
        except Exception:  # noqa: BLE001 - telemetry never changes an evaluation
            _logger.debug("feature_flag_metric_failed", extra={"flag": hook_context.flag_key}, exc_info=True)


def _check_exposure_budget(value: Any) -> None:
    """Inspect at most 10,000 value occurrences before copying; repeated references count again."""
    remaining = 10_000
    stack = [iter((value,))]
    while stack:
        try:
            member = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if remaining == 0:
            raise ValueError("Exposure snapshot exceeds 10000 value occurrences")
        remaining -= 1
        if isinstance(member, Mapping):
            stack.append(iter(member.values()))
        elif isinstance(member, list | tuple):
            stack.append(iter(member))


def _log_exposure_failure(flag: str) -> None:
    with suppress(Exception):  # logging is best effort too
        _logger.debug("feature_flag_exposure_failed", extra={"flag": flag}, exc_info=True)


class ExposureEventHook(Hook):
    """Publishes one ``FeatureFlagEvaluated`` per evaluation (see the module documentation)."""

    def __init__(self, publisher: ApplicationEventPublisher) -> None:
        self._publisher = publisher
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        # scheduled publishes until they settle: a task of the running loop, or a future of the application's loop
        self._pending: set[asyncio.Task[None] | concurrent.futures.Future[None]] = set()
        self._lock = threading.Lock()  # the done callbacks and the evaluations run on several threads

    def finally_after(
        self, hook_context: HookContext, details: FlagEvaluationDetails[FlagValueType], hints: HookHints
    ) -> None:
        if hints.get(PREVIEW_HINT):
            return
        try:
            value = details.value
            _check_exposure_budget(value)
            if isinstance(value, Mapping | list | tuple):  # the caller owns details.value: publish what was served
                value = copy.deepcopy(value)
            event = FeatureFlagEvaluated(
                key=hook_context.flag_key,
                value=value,
                variant=None if _failed(details) else details.variant,
                reason=_reason(details),
                error_code=details.error_code.value if details.error_code is not None else None,
                targeting_key=hook_context.evaluation_context.targeting_key,
            )
            self._dispatch(event)
        except Exception:  # noqa: BLE001 - telemetry never changes an evaluation
            _log_exposure_failure(hook_context.flag_key)

    def _dispatch(self, event: FeatureFlagEvaluated) -> None:
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            self._track(running.create_task(self._publish(event)))
            return
        application_loop = self._loop
        if application_loop is not None and application_loop.is_running():
            publish = self._publish(event)
            try:
                self._track(asyncio.run_coroutine_threadsafe(publish, application_loop))
                return
            except RuntimeError:
                publish.close()  # the application's loop closed under us: nobody is left to run it
        asyncio.run(self._publish(event))

    def _track(self, handle: asyncio.Task[None] | concurrent.futures.Future[None]) -> None:
        with self._lock:
            self._pending.add(handle)
        handle.add_done_callback(self._settled)

    def _settled(self, handle: asyncio.Future[Any] | concurrent.futures.Future[Any]) -> None:
        with self._lock:
            self._pending.discard(cast("Any", handle))
        if handle.cancelled():
            _logger.debug("feature_flag_exposure_cancelled")

    async def _publish(self, event: FeatureFlagEvaluated) -> None:
        try:
            await self._publisher.publish(event)
        except Exception:  # noqa: BLE001 - a listener never breaks an evaluation
            _log_exposure_failure(event.key)

    async def drain(self) -> None:
        """Await every publish scheduled so far (the binding calls it when the context stops).

        Publishes that run on the calling loop, or on the application's loop, are awaited; one scheduled on any
        other loop belongs to that loop and is left to it.
        """
        current = asyncio.get_running_loop()
        while True:
            with self._lock:
                handles = list(self._pending)
            waits = [
                asyncio.wrap_future(handle) if isinstance(handle, concurrent.futures.Future) else handle
                for handle in handles
                if isinstance(handle, concurrent.futures.Future) or handle.get_loop() is current
            ]
            if not waits:
                return
            await asyncio.gather(*waits, return_exceptions=True)
