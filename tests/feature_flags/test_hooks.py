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
"""MetricsHook and ExposureEventHook (spec 4.9): best effort, never change an evaluation."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from openfeature.client import OpenFeatureClient
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails, FlagEvaluationOptions, FlagType, Reason
from openfeature.hook import HookContext
from prometheus_client import REGISTRY

from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher
from pyfly.feature_flags.client import PREVIEW_HINT, FeatureFlags
from pyfly.feature_flags.context import EvaluationContextResolver
from pyfly.feature_flags.events import FeatureFlagEvaluated
from pyfly.feature_flags.hooks import EVALUATIONS_METRIC, ExposureEventHook, MetricsHook
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.observability.metrics import MetricsRegistry
from tests.feature_flags.support import bool_flag, bound_client, recording_publisher, wait_until

HOOKS_LOGGER = "pyfly.feature_flags.hooks"


class FakeCounter:
    def __init__(self) -> None:
        self.increments: list[dict[str, str]] = []
        self._labels: dict[str, str] = {}

    def labels(self, **labels: str) -> FakeCounter:
        self._labels = labels
        return self

    def inc(self) -> None:
        self.increments.append(self._labels)


class FakeRecorder:
    def __init__(self) -> None:
        self.counter_args: tuple[str, str, list[str] | None] | None = None
        self.instrument = FakeCounter()

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> FakeCounter:
        self.counter_args = (name, description, labels)
        return self.instrument

    def histogram(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def gauge(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class RacingRecorder(FakeRecorder):
    """``counter()`` waits (at most 0.3 s) for a second thread to be inside it too, and records whether one was.

    A registry whose ``counter()`` is check-then-create loses the race exactly when two threads are inside it at
    once. The wait is a bounded rendezvous, not a pause: it ends the moment the second thread arrives, and only the
    thread that is alone (the hook serializing the creation) waits the whole 0.3 s.
    """

    def __init__(self) -> None:
        super().__init__()
        self.created = 0
        self.overlapped = False
        self._rendezvous = threading.Barrier(2)

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> FakeCounter:
        self.created += 1
        try:
            self._rendezvous.wait(timeout=0.3)
            self.overlapped = True
        except threading.BrokenBarrierError:
            pass
        return super().counter(name, description, labels)


class RaisingPublisher:
    """A publisher whose bus is down: every publish raises."""

    def __init__(self) -> None:
        self.attempts = 0

    async def publish(self, event: object) -> None:
        self.attempts += 1
        raise RuntimeError("bus down")


class GatedPublisher:
    """A publisher whose publish starts, then waits until the test opens the gate."""

    def __init__(self) -> None:
        self.started = 0
        self.gate = asyncio.Event()
        self.delivered: list[object] = []

    async def publish(self, event: object) -> None:
        self.started += 1
        await self.gate.wait()
        self.delivered.append(event)


class LoopRecordingPublisher:
    """A publisher recording the loop (and thread) each publish runs on."""

    def __init__(self) -> None:
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.threads: list[int] = []

    async def publish(self, event: object) -> None:
        self.loops.append(asyncio.get_running_loop())
        self.threads.append(threading.get_ident())


def _provider() -> FireflyFlagProvider:
    provider = FireflyFlagProvider()
    provider.update({"flags": {"a": bool_flag(), "off": {**bool_flag(), "state": "DISABLED"}}})
    return provider


def _debug_records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == HOOKS_LOGGER and r.getMessage() == event]


def test_the_metric_counts_flag_variant_and_reason() -> None:
    recorder = FakeRecorder()
    with bound_client(_provider(), hooks=[MetricsHook(recorder)]) as client:
        client.get_boolean_value("a", False)
        client.get_boolean_value("off", False)
        client.get_boolean_value("missing", False)
        client.get_string_value("a", "x")  # type mismatch
        client.get_boolean_value("a", False, None, FlagEvaluationOptions(hook_hints={PREVIEW_HINT: True}))
    assert recorder.counter_args is not None and recorder.counter_args[0] == EVALUATIONS_METRIC
    assert recorder.counter_args[2] == ["flag", "variant", "reason"]
    assert recorder.instrument.increments == [
        {"flag": "a", "variant": "on", "reason": "STATIC"},
        {"flag": "off", "variant": "none", "reason": "DISABLED"},
        {"flag": "missing", "variant": "none", "reason": "ERROR"},
        {"flag": "a", "variant": "none", "reason": "ERROR"},
    ]  # the preview evaluation is not counted


def test_a_failing_recorder_never_changes_the_evaluation(caplog: pytest.LogCaptureFixture) -> None:
    class Exploding(FakeCounter):
        def inc(self) -> None:
            raise RuntimeError("registry down")

    recorder = FakeRecorder()
    recorder.instrument = Exploding()
    with (
        caplog.at_level(logging.DEBUG, logger=HOOKS_LOGGER),
        bound_client(_provider(), hooks=[MetricsHook(recorder)]) as client,
    ):
        details = client.get_boolean_details("a", False)
    assert details.value is True and details.error_code is None
    failures = _debug_records(caplog, "feature_flag_metric_failed")
    assert len(failures) == 1 and failures[0].levelno == logging.DEBUG and failures[0].exc_info is not None


def test_a_recorder_that_cannot_create_the_counter_never_changes_the_evaluation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class NoCounter(FakeRecorder):
        def counter(self, name: str, description: str, labels: list[str] | None = None) -> FakeCounter:
            raise RuntimeError("duplicated timeseries")

    with (
        caplog.at_level(logging.DEBUG, logger=HOOKS_LOGGER),
        bound_client(_provider(), hooks=[MetricsHook(NoCounter())]) as client,
    ):
        assert client.get_boolean_value("a", False) is True
    assert len(_debug_records(caplog, "feature_flag_metric_failed")) == 1


def test_a_failing_metrics_lookup_never_changes_the_evaluation(caplog: pytest.LogCaptureFixture) -> None:
    def lookup() -> FakeRecorder:
        raise RuntimeError("container not ready")

    with (
        caplog.at_level(logging.DEBUG, logger=HOOKS_LOGGER),
        bound_client(_provider(), hooks=[MetricsHook(lookup)]) as client,
    ):
        assert client.get_boolean_value("a", False) is True
    assert len(_debug_records(caplog, "feature_flag_metric_failed")) == 1


def test_an_error_reason_is_counted_as_an_error_even_when_the_provider_names_no_error_code() -> None:
    recorder = FakeRecorder()
    hook = MetricsHook(recorder)
    context = HookContext("flaky", FlagType.BOOLEAN, False, EvaluationContext())
    hook.finally_after(
        context, FlagEvaluationDetails(flag_key="flaky", value=False, variant="on", reason=Reason.ERROR), {}
    )
    assert recorder.instrument.increments == [{"flag": "flaky", "variant": "none", "reason": "ERROR"}]


def test_two_threads_on_the_first_evaluation_create_the_counter_once() -> None:
    recorder = RacingRecorder()
    hook = MetricsHook(recorder)
    with bound_client(_provider(), hooks=[hook]) as client, ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: client.get_boolean_value("a", False), range(2)))
    assert recorder.created == 1 and not recorder.overlapped
    assert len(recorder.instrument.increments) == 2  # neither count is lost


def test_a_recorder_looked_up_lazily_is_used_once_it_exists() -> None:
    recorder = FakeRecorder()
    available: list[FakeRecorder] = []
    with bound_client(_provider(), hooks=[MetricsHook(lambda: available[0] if available else None)]) as client:
        client.get_boolean_value("a", False)  # no recorder yet: not counted, nothing breaks
        available.append(recorder)
        client.get_boolean_value("a", False)
    assert recorder.instrument.increments == [{"flag": "a", "variant": "on", "reason": "STATIC"}]


def test_a_recorder_found_by_the_lookup_is_looked_up_only_once() -> None:
    recorder = FakeRecorder()
    lookups: list[int] = []

    def lookup() -> FakeRecorder:
        lookups.append(1)
        return recorder

    with bound_client(_provider(), hooks=[MetricsHook(lookup)]) as client:
        client.get_boolean_value("a", False)
        client.get_boolean_value("a", False)
    assert len(lookups) == 1 and len(recorder.instrument.increments) == 2


def test_without_a_recorder_the_hook_does_nothing() -> None:
    with bound_client(_provider(), hooks=[MetricsHook(None)]) as client:
        assert client.get_boolean_value("a", False) is True


def test_the_prometheus_counter_is_feature_flag_evaluations_total() -> None:
    labels = {"flag": "a", "variant": "on", "reason": "STATIC"}
    before = REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) or 0.0
    with bound_client(_provider(), hooks=[MetricsHook(MetricsRegistry())]) as client:
        client.get_boolean_value("a", False)
    assert REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) == before + 1


async def test_exposure_events_are_published_on_the_running_loop() -> None:
    publisher, seen = recording_publisher()
    hook = ExposureEventHook(publisher)
    with bound_client(_provider(), hooks=[hook]) as client:
        client.get_boolean_details("a", False, None)
        await client.get_boolean_details_async("missing", True)
        client.get_boolean_value("a", False, None, FlagEvaluationOptions(hook_hints={PREVIEW_HINT: True}))
    await hook.drain()
    assert seen == [
        FeatureFlagEvaluated("a", True, "on", "STATIC", None, None),
        FeatureFlagEvaluated("missing", True, None, "ERROR", "FLAG_NOT_FOUND", None),
    ]


async def test_an_evaluation_on_a_worker_thread_reaches_the_application_loop() -> None:
    publisher, seen = recording_publisher()
    hook = ExposureEventHook(publisher)  # created on the application's loop
    with bound_client(_provider(), hooks=[hook]) as client:
        await asyncio.to_thread(client.get_boolean_value, "a", False)
    await hook.drain()
    assert [event.key for event in seen if isinstance(event, FeatureFlagEvaluated)] == ["a"]


def test_without_any_loop_the_event_is_published_through_a_short_lived_one() -> None:
    publisher, seen = recording_publisher()
    hook = ExposureEventHook(publisher)  # no running loop here (a CLI script)
    with bound_client(_provider(), hooks=[hook]) as client:
        client.get_boolean_value("a", False)
    assert [event.key for event in seen if isinstance(event, FeatureFlagEvaluated)] == ["a"]


def test_after_the_application_loop_stopped_the_event_goes_through_a_short_lived_one() -> None:
    publisher, seen = recording_publisher()
    loop = asyncio.new_event_loop()
    try:

        async def create() -> ExposureEventHook:
            return ExposureEventHook(publisher)

        hook = loop.run_until_complete(create())  # captures a loop that is not running any more afterwards
    finally:
        loop.close()
    with bound_client(_provider(), hooks=[hook]) as client:
        client.get_boolean_value("a", False)
    assert [event.key for event in seen if isinstance(event, FeatureFlagEvaluated)] == ["a"]


async def test_an_application_loop_that_closes_under_a_worker_thread_does_not_lose_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, seen = recording_publisher()
    hook = ExposureEventHook(publisher)

    def closed(coro: Any, loop: asyncio.AbstractEventLoop) -> Any:
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr("pyfly.feature_flags.hooks.asyncio.run_coroutine_threadsafe", closed)
    with bound_client(_provider(), hooks=[hook]) as client:
        await asyncio.to_thread(client.get_boolean_value, "a", False)
    assert [event.key for event in seen if isinstance(event, FeatureFlagEvaluated)] == ["a"]


async def test_a_worker_thread_publishes_on_the_application_loop_not_on_a_loop_of_its_own() -> None:
    publisher = LoopRecordingPublisher()
    hook = ExposureEventHook(publisher)  # type: ignore[arg-type]
    with bound_client(_provider(), hooks=[hook]) as client:
        worker = await asyncio.to_thread(lambda: (client.get_boolean_value("a", False), threading.get_ident())[1])
    await hook.drain()
    assert publisher.loops == [asyncio.get_running_loop()]
    assert publisher.threads == [threading.get_ident()] and publisher.threads != [worker]


def test_an_error_reason_is_published_without_a_variant_even_when_the_provider_names_no_error_code() -> None:
    publisher, seen = recording_publisher()
    hook = ExposureEventHook(publisher)
    context = HookContext("flaky", FlagType.BOOLEAN, False, EvaluationContext(targeting_key="u-1"))
    details = FlagEvaluationDetails(flag_key="flaky", value=False, variant="on", reason=Reason.ERROR)
    hook.finally_after(context, details, {})
    assert seen == [FeatureFlagEvaluated("flaky", False, None, "ERROR", None, "u-1")]


def test_the_targeting_key_of_the_evaluation_is_recorded() -> None:
    publisher, seen = recording_publisher()
    with bound_client(_provider(), hooks=[ExposureEventHook(publisher)]) as client:
        client.get_boolean_value("a", False, EvaluationContext(targeting_key="u-9"))
    assert isinstance(seen[0], FeatureFlagEvaluated) and seen[0].targeting_key == "u-9"


def test_hooks_attach_to_one_client_only() -> None:
    recorder = FakeRecorder()
    provider = _provider()
    with bound_client(provider, hooks=[MetricsHook(recorder)]) as client:
        other = OpenFeatureClient(domain=client.domain, version=None)  # third-party client, same provider
        other.get_boolean_value("a", False)
    assert recorder.instrument.increments == []


# -- hooks never change an evaluation ----------------------------------------------------------------------


def test_a_publisher_that_raises_never_changes_the_evaluation_without_a_loop(caplog: pytest.LogCaptureFixture) -> None:
    publisher = RaisingPublisher()
    hook = ExposureEventHook(publisher)  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG, logger=HOOKS_LOGGER), bound_client(_provider(), hooks=[hook]) as client:
        details = client.get_boolean_details("a", False)
    assert details.value is True and details.variant == "on" and details.error_code is None
    assert publisher.attempts == 1
    failures = _debug_records(caplog, "feature_flag_exposure_failed")
    assert len(failures) == 1 and failures[0].levelno == logging.DEBUG and failures[0].exc_info is not None


async def test_a_publisher_that_raises_never_changes_the_evaluation_on_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = RaisingPublisher()
    hook = ExposureEventHook(publisher)  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG, logger=HOOKS_LOGGER):
        with bound_client(_provider(), hooks=[hook]) as client:
            sync_details = client.get_boolean_details("a", False)
            async_details = await client.get_boolean_details_async("a", False)
        await hook.drain()
    assert sync_details.value is True and async_details.value is True
    assert publisher.attempts == 2
    assert len(_debug_records(caplog, "feature_flag_exposure_failed")) == 2


async def test_a_publisher_that_raises_never_changes_the_evaluation_on_a_worker_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = RaisingPublisher()
    hook = ExposureEventHook(publisher)  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG, logger=HOOKS_LOGGER):
        with bound_client(_provider(), hooks=[hook]) as client:
            value = await asyncio.to_thread(client.get_boolean_value, "a", False)
        await hook.drain()
    assert value is True and publisher.attempts == 1
    assert len(_debug_records(caplog, "feature_flag_exposure_failed")) == 1


async def test_the_management_preview_records_no_metric_and_no_exposure_event() -> None:
    recorder = FakeRecorder()
    publisher, seen = recording_publisher()
    hook = ExposureEventHook(publisher)
    with bound_client(_provider(), hooks=[MetricsHook(recorder), hook]) as client:
        facade = FeatureFlags(client, EvaluationContextResolver())
        preview = facade.details("a", False, ambient=False, preview=True)
        async_preview = await facade.details_async("a", False, ambient=False, preview=True)
        assert preview.value is True and async_preview.value is True
        facade.details("a", False)  # a real evaluation is recorded
        await facade.details_async("off", True)
    await hook.drain()
    assert recorder.instrument.increments == [
        {"flag": "a", "variant": "on", "reason": "STATIC"},
        {"flag": "off", "variant": "none", "reason": "DISABLED"},
    ]
    assert [event.key for event in seen if isinstance(event, FeatureFlagEvaluated)] == ["a", "off"]


# -- scheduled publishes are kept, awaited on drain, and never lost silently ----------------------------------


async def _let_the_loop_run(turns: int = 25) -> None:
    """Give every other ready task (a ``drain`` that returns at once included) real chances to run: one turn
    each. ``wait_until`` would not do, it returns without yielding when its predicate already holds."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_drain_waits_for_a_publish_that_is_still_running() -> None:
    publisher = GatedPublisher()
    hook = ExposureEventHook(publisher)  # type: ignore[arg-type]
    with bound_client(_provider(), hooks=[hook]) as client:
        client.get_boolean_value("a", False)
    await wait_until(lambda: publisher.started == 1)  # the publish runs and waits for the gate
    draining = asyncio.create_task(hook.drain())
    await _let_the_loop_run()
    assert not draining.done()  # still waiting for the listener
    assert publisher.delivered == []
    publisher.gate.set()
    await asyncio.wait_for(draining, 5)
    assert [event.key for event in publisher.delivered if isinstance(event, FeatureFlagEvaluated)] == ["a"]


async def test_drain_with_nothing_scheduled_returns_at_once() -> None:
    publisher, seen = recording_publisher()
    await ExposureEventHook(publisher).drain()
    assert seen == []


async def test_drain_waits_for_the_publish_of_a_worker_thread() -> None:
    publisher = GatedPublisher()
    hook = ExposureEventHook(publisher)  # type: ignore[arg-type]
    assert not publisher.gate.is_set()  # the gate is held before the evaluation in the thread
    with bound_client(_provider(), hooks=[hook]) as client:
        await asyncio.to_thread(client.get_boolean_value, "a", False)
    await wait_until(lambda: publisher.started == 1)  # handed to the application loop, waiting for the gate
    draining = asyncio.create_task(hook.drain())
    await _let_the_loop_run()
    assert not draining.done()
    assert publisher.delivered == []
    publisher.gate.set()
    await asyncio.wait_for(draining, 5)
    assert [event.key for event in publisher.delivered if isinstance(event, FeatureFlagEvaluated)] == ["a"]


async def test_a_publish_that_is_cancelled_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    publisher = GatedPublisher()
    hook = ExposureEventHook(publisher)  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG, logger=HOOKS_LOGGER):
        with bound_client(_provider(), hooks=[hook]) as client:
            client.get_boolean_value("a", False)
        await wait_until(lambda: publisher.started == 1)
        for task in asyncio.all_tasks():
            if getattr(task.get_coro(), "__qualname__", "").endswith("ExposureEventHook._publish"):
                task.cancel()  # the application is stopping: the in-flight publish is cancelled
        await hook.drain()
    assert publisher.delivered == []
    assert len(_debug_records(caplog, "feature_flag_exposure_cancelled")) == 1


async def test_a_listener_that_mutates_the_exposed_value_never_changes_the_flag() -> None:
    """The exposure event carries the evaluation's own copy: a listener that mutates it corrupts no later evaluation
    (nor the definition the provider stores)."""
    banner = {"state": "ENABLED", "variants": {"plain": {"title": "Hi", "tags": ["a"]}}, "defaultVariant": "plain"}
    provider = FireflyFlagProvider()
    provider.update({"flags": {"banner": banner}})
    bus = ApplicationEventBus()
    exposed: list[Any] = []

    async def vandal(event: FeatureFlagEvaluated) -> None:
        event.value["title"] = "changed"  # type: ignore[index]
        event.value["tags"].append("b")  # type: ignore[index]
        exposed.append(event.value)

    bus.subscribe(FeatureFlagEvaluated, vandal)
    hook = ExposureEventHook(ApplicationEventPublisher(bus))
    with bound_client(provider, hooks=[hook]) as client:
        facade = FeatureFlags(client, EvaluationContextResolver())
        facade.get_object("banner", {})
        await hook.drain()
        assert exposed == [{"title": "changed", "tags": ["a", "b"]}]
        assert facade.get_object("banner", {}) == {"title": "Hi", "tags": ["a"]}
        assert client.get_object_value("banner", {}) == {"title": "Hi", "tags": ["a"]}
    assert provider.definition("banner") == banner
