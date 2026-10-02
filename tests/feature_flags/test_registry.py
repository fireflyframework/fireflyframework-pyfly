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
"""FlagRegistry: per-key composition, last good document, change detection, polling (spec 4.5)."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

import pytest
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEvent, ProviderEventDetails
from openfeature.exception import ErrorCode, ParseError
from openfeature.provider import FeatureProvider

from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher, ContextRefreshedEvent
from pyfly.data.relational.migrations import MIGRATION_PHASE
from pyfly.data.relational.schema import SCHEMA_PHASE
from pyfly.feature_flags.definitions import FlagDefinitionError, FlagDocument, parse_document
from pyfly.feature_flags.events import FeatureFlagsChanged
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FEATURE_FLAGS_PHASE, TEST_OVERRIDES, FeatureFlagsError, FlagRegistry
from pyfly.feature_flags.sources import FlagSourceError, SourceSnapshot
from pyfly.kernel.lifecycle import DEFAULT_PHASE, lifecycle_phase
from tests.feature_flags.support import (
    DocumentSource,
    ScriptedSource,
    StaticSource,
    bool_flag,
    bound_client,
    recording_publisher,
    wait_until,
)

_REGISTRY_LOGGER = "pyfly.feature_flags.registry"


def _refused_document(key: str) -> FlagDocument:
    """A document FlagdCore refuses (``state`` is not ENABLED/DISABLED); ``parse_document`` would have rejected it."""
    return FlagDocument(flags={key: {"state": "ARCHIVED", "variants": {"on": True, "off": False}}})


def _minutes() -> Callable[[], dt.datetime]:
    """A clock that moves one minute per reading, from 2026-10-02T09:00Z."""
    ticks = (dt.datetime(2026, 10, 2, 9, 0, tzinfo=dt.UTC) + dt.timedelta(minutes=n) for n in range(1000))
    return lambda: next(ticks)


def _configuration_changes(provider: FireflyFlagProvider) -> list[list[str] | None]:
    """The keys of every PROVIDER_CONFIGURATION_CHANGED *provider* emits from now on."""
    signals: list[list[str] | None] = []

    def on_emit(_: FeatureProvider, event: ProviderEvent, details: ProviderEventDetails) -> None:
        if event is ProviderEvent.PROVIDER_CONFIGURATION_CHANGED:
            signals.append(details.flags_changed)

    provider.attach(on_emit)
    return signals


async def test_the_highest_source_wins_and_the_provider_evaluates_it() -> None:
    provider = FireflyFlagProvider()
    registry = FlagRegistry(
        [StaticSource("config", {"a": True, "b": "blue"}), StaticSource("file", {"a": bool_flag("off")})], provider
    )
    await registry.start()
    flag = registry.effective_flag("a")
    assert flag is not None and (flag.origin, flag.overrides) == ("file", ("config",))
    assert provider.definition("a") == bool_flag("off")
    assert registry.layers("a") == [("config", bool_flag("on")), ("file", bool_flag("off"))]
    assert registry.document()["flags"]["b"]["defaultVariant"] == "blue"
    await registry.stop()


async def test_a_fail_fast_source_that_cannot_load_fails_startup_with_the_key_and_the_reason() -> None:
    bad = ScriptedSource("config", FlagDefinitionError("bad key", "invalid flag key"), fail_fast=True)
    registry = FlagRegistry([bad], FireflyFlagProvider())
    with pytest.raises(
        FlagSourceError, match="'config' failed to load: invalid feature flag 'bad key': invalid flag key"
    ):
        await registry.start()


async def test_a_remote_source_that_cannot_load_at_startup_contributes_nothing() -> None:
    registry = FlagRegistry(
        [StaticSource("config", {"a": True}), ScriptedSource("http", ConnectionError("refused"))], FireflyFlagProvider()
    )
    await registry.start()
    statuses = {status.name: status for status in registry.sources()}
    assert statuses["config"].status == "UP" and statuses["config"].flags == 1
    assert statuses["http"].status == "DOWN" and statuses["http"].error == "ConnectionError: refused"
    assert set(registry.composition().flags) == {"a"}
    await registry.stop()


async def test_a_failed_refresh_keeps_the_last_good_document() -> None:
    http = ScriptedSource("http", {"a": bool_flag("off")}, TimeoutError("slow"), {"a": bool_flag("on")})
    registry = FlagRegistry([http], FireflyFlagProvider())
    await registry.start()
    assert await registry.refresh("http") == []
    [status] = registry.sources()
    assert status.status == "STALE" and status.error == "TimeoutError: slow"
    assert registry.provider.definition("a") == bool_flag("off")
    assert await registry.refresh("http") == ["a"]
    assert registry.sources()[0].status == "UP" and registry.sources()[0].error is None
    await registry.stop()


async def test_changes_publish_feature_flags_changed_with_the_changed_keys() -> None:
    publisher, seen = recording_publisher()
    file = ScriptedSource("file", {"a": True, "b": True}, None, {"a": False, "b": True})
    registry = FlagRegistry([file], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    assert seen == [FeatureFlagsChanged(("a", "b"), "startup")]
    assert await registry.refresh("file") == []  # unchanged: nothing published
    assert await registry.refresh("file") == ["a"]
    assert seen[-1] == FeatureFlagsChanged(("a",), "file")
    await registry.stop()


@pytest.mark.parametrize(
    "after",
    [
        bool_flag(metadata={"expires": "2027-06-30"}),
        bool_flag(metadata={"expires": "2027-01-01"}, notes="see ADR-12"),  # a field flagd never reads
        {**bool_flag(metadata={"expires": "2027-01-01"}), "variants": {"on": 1, "off": 0}},  # True == 1 in Python
    ],
    ids=["metadata-only", "unread-field", "variant-type"],
)
async def test_any_change_to_a_composed_definition_publishes_its_key(after: dict[str, Any]) -> None:
    publisher, seen = recording_publisher()
    before = bool_flag(metadata={"expires": "2027-01-01"})
    file = DocumentSource("file", {"flags": {"a": before, "b": bool_flag()}}, {"flags": {"a": after, "b": bool_flag()}})
    registry = FlagRegistry([file], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    assert await registry.refresh("file") == ["a"]
    assert seen[-1] == FeatureFlagsChanged(("a",), "file")
    await registry.stop()


async def test_an_evaluator_change_publishes_the_keys_that_depend_on_it() -> None:
    def document(beta_roles: list[str]) -> dict[str, Any]:
        return {
            "flags": {
                "direct": bool_flag("off", targeting={"if": [{"$ref": "is-beta"}, "on", "off"]}),
                "nested": bool_flag("off", targeting={"if": [{"$ref": "vip"}, "on", "off"]}),
                "other": bool_flag("off", targeting={"if": [{"$ref": "is-staff"}, "on", "off"]}),
                "static": bool_flag(),
                # Only targeting is expanded: a variant that looks like a reference is a value.
                "literal": {"state": "ENABLED", "variants": {"ref": {"$ref": "is-beta"}, "none": {}}},
                # A reference is an object with exactly one key: this one is not, so it depends on nothing.
                "lookalike": bool_flag("off", targeting={"if": [{"$ref": "is-beta", "other": 1}, "on", "off"]}),
            },
            "$evaluators": {
                "is-beta": {"in": [{"var": "role"}, beta_roles]},
                "is-staff": {"==": [{"var": "role"}, "staff"]},
                # "vip" sorts after the evaluators it $refs: the provider resolves them whatever the order.
                "vip": {"or": [{"$ref": "is-staff"}, {"$ref": "is-beta"}]},
            },
        }

    publisher, seen = recording_publisher()
    file = DocumentSource("file", document(["beta"]), document(["beta", "tester"]))
    registry = FlagRegistry([file], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    tester = EvaluationContext("t-1", {"role": "tester"})
    assert registry.provider.resolve_boolean_details("nested", False, tester).value is False
    assert await registry.refresh("file") == ["direct", "nested"]
    assert seen[-1] == FeatureFlagsChanged(("direct", "nested"), "file")
    assert registry.provider.resolve_boolean_details("nested", False, tester).value is True
    await registry.stop()


async def test_a_document_metadata_change_publishes_the_keys_that_inherit_it() -> None:
    def document(owner: str) -> dict[str, Any]:
        own = bool_flag(metadata={"owner": "payments"})
        return {"flags": {"inherits": bool_flag(), "own": own}, "metadata": {"owner": owner}}

    publisher, seen = recording_publisher()
    file = DocumentSource("file", document("platform"), document("checkout"))
    registry = FlagRegistry([file], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    assert await registry.refresh("file") == ["inherits"]  # "own" overrides the document's owner
    assert seen[-1] == FeatureFlagsChanged(("inherits",), "file")
    assert registry.provider.resolve_boolean_details("inherits", False).flag_metadata == {"owner": "checkout"}
    await registry.stop()


async def test_a_recomposition_that_changes_nothing_publishes_nothing() -> None:
    document = {
        "flags": {"a": bool_flag("off", targeting={"if": [{"$ref": "is-beta"}, "on", "off"]})},
        "$evaluators": {"is-beta": {"in": [{"var": "role"}, ["beta"]]}},
        "metadata": {"owner": "platform"},
    }
    publisher, seen = recording_publisher()
    config = DocumentSource("config", {"flags": {"a": bool_flag("on")}}, {"flags": {"a": bool_flag("off")}})
    file = DocumentSource("file", document, copy.deepcopy(document))
    registry = FlagRegistry([config, file], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    assert await registry.refresh("file") == []  # the same document again
    assert await registry.refresh("config") == []  # a change the file layer shadows
    assert registry.layers("a")[0] == ("config", bool_flag("off"))  # both were composed
    assert seen == [FeatureFlagsChanged(("a",), "startup")]
    await registry.stop()


async def test_a_document_the_provider_refuses_leaves_the_previous_set_in_force(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher, seen = recording_publisher()
    off, on = {"flags": {"a": bool_flag("off")}}, {"flags": {"a": bool_flag("on")}}
    http = DocumentSource("http", off, _refused_document("a"), None, on)
    registry = FlagRegistry([http], FireflyFlagProvider(), publisher=publisher, clock=_minutes())
    await registry.start()
    composition, document = registry.composition(), registry.provider.document
    [loaded] = registry.sources()
    with caplog.at_level(logging.ERROR, logger=_REGISTRY_LOGGER):
        assert await registry.refresh("http") == []
    assert [r.getMessage() for r in caplog.records] == ["feature_flag_provider_update_failed"]
    assert registry.composition() is composition and registry.provider.document == document
    assert registry.layers("a") == [("http", bool_flag("off"))]  # the source keeps its last good document
    [status] = registry.sources()
    assert status.status == "STALE" and status.revision == "1"
    assert status.last_refresh == loaded.last_refresh == dt.datetime(2026, 10, 2, 9, 0, tzinfo=dt.UTC)
    assert status.error is not None and status.error.startswith("ParseError: ")
    assert await registry.refresh("http") == []  # "unchanged" is still the refused document
    assert registry.sources()[0].status == "STALE"
    assert seen == [FeatureFlagsChanged(("a",), "startup")]
    assert await registry.refresh("http") == ["a"]  # a new document applies
    assert registry.sources()[0].status == "UP" and registry.provider.definition("a") == bool_flag("on")
    await registry.stop()


async def test_a_refused_boot_composition_fails_startup(caplog: pytest.LogCaptureFixture) -> None:
    """Spec 4.5 fails startup on an invalid boot definition; which layer FlagdCore objects to is unknown, so the error
    names the provider's reason rather than a source."""
    publisher, seen = recording_publisher()
    provider = FireflyFlagProvider()
    file = DocumentSource("file", {"flags": {"a": bool_flag()}}, refresh_interval=0.01)
    registry = FlagRegistry([DocumentSource("config", _refused_document("bad")), file], provider, publisher=publisher)
    with (
        caplog.at_level(logging.ERROR, logger=_REGISTRY_LOGGER),
        pytest.raises(FeatureFlagsError, match=r"refused the startup composition: ParseError: ") as raised,
    ):
        await registry.start()
    assert isinstance(raised.value.__cause__, ParseError)
    assert [r.getMessage() for r in caplog.records] == ["feature_flag_provider_update_failed"]
    assert seen == [] and provider.document == {"flags": {}} and registry.composition().flags == {}
    assert not registry.started
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("pyfly-feature-flags-")]


async def test_a_refused_test_override_change_is_rolled_back_and_raised() -> None:
    """A test override shadows a definition FlagdCore refuses; replacing or clearing it would expose that one."""
    publisher, seen = recording_publisher()
    provider = FireflyFlagProvider()
    http = DocumentSource("http", {"flags": {"a": bool_flag("off")}}, _refused_document("a"))
    registry = FlagRegistry([DocumentSource("store", _refused_document("bad")), http], provider, publisher=publisher)
    assert registry.set_test_overrides({"bad": True}) == ["bad"]
    await registry.start()
    composition, document = registry.composition(), provider.document
    for change in (lambda: registry.set_test_overrides({"other": True}), registry.clear_test_overrides):
        with pytest.raises(FeatureFlagsError, match=r"refused the test overrides: ParseError: ") as raised:
            change()
        assert isinstance(raised.value.__cause__, ParseError)
        assert registry.test_overrides == {"bad": True}
        flag = registry.effective_flag("bad")
        assert flag is not None and flag.origin == TEST_OVERRIDES
        assert registry.composition() is composition and provider.document == document
    assert await registry.refresh("http") == []  # blamed on the source that brought it: the layers were restored
    assert {status.name: status.status for status in registry.sources()} == {"store": "UP", "http": "STALE"}
    await registry.stop()  # drains the override event, published on the loop
    assert set(seen) == {FeatureFlagsChanged(("bad",), TEST_OVERRIDES), FeatureFlagsChanged(("a",), "startup")}
    assert len(seen) == 2  # a refused change publishes nothing


async def test_a_refusal_outlives_a_failed_load_until_a_new_document_arrives() -> None:
    """refused -> timeout -> unchanged: the source still serves the refused document, so the refusal is the reason."""
    off, on = {"flags": {"a": bool_flag("off")}}, {"flags": {"a": bool_flag("on")}}
    http = DocumentSource("http", off, _refused_document("a"), TimeoutError("slow"), None, on)
    registry = FlagRegistry([http], FireflyFlagProvider())
    await registry.start()
    errors: list[tuple[str, str | None]] = []
    for _ in range(4):
        await registry.refresh("http")
        [status] = registry.sources()
        errors.append((status.status, status.error and status.error.split(":")[0]))
    assert errors == [("STALE", "ParseError"), ("STALE", "TimeoutError"), ("STALE", "ParseError"), ("UP", None)]
    assert registry.provider.definition("a") == bool_flag("on")
    await registry.stop()


async def test_a_refusal_blamed_on_a_source_is_logged_with_its_name(caplog: pytest.LogCaptureFixture) -> None:
    http = DocumentSource("http", {"flags": {"a": bool_flag()}}, _refused_document("a"))
    registry = FlagRegistry([StaticSource("config", {"b": True}), http], FireflyFlagProvider())
    await registry.start()
    with caplog.at_level(logging.WARNING, logger=_REGISTRY_LOGGER):
        await registry.refresh("http")
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.ERROR, "feature_flag_provider_update_failed"),
        (logging.WARNING, "feature_flag_source_failed"),
    ]
    blamed: Any = caplog.records[1]
    assert blamed.source == "http"
    assert blamed.error == registry.sources()[1].error and blamed.error.startswith("ParseError: ")
    await registry.stop()


async def test_the_provider_signals_the_keys_the_registry_publishes() -> None:
    """One change signal: OpenFeature handlers see FeatureFlagsChanged's keys, not FlagdCore's own diff, which misses
    the document metadata a flag inherits and the fields flagd does not read."""

    def document(owner: str, notes: str) -> dict[str, Any]:
        flags = {"inherits": bool_flag(), "noted": bool_flag(notes=notes), "own": bool_flag(metadata={"owner": "x"})}
        return {"flags": flags, "metadata": {"owner": owner}}

    provider = FireflyFlagProvider()
    signals = _configuration_changes(provider)
    publisher, seen = recording_publisher()
    file = DocumentSource("file", document("platform", "v1"), document("checkout", "v2"), document("checkout", "v2"))
    registry = FlagRegistry([file], provider, publisher=publisher)
    await registry.start()
    assert await registry.refresh("file") == ["inherits", "noted"]
    assert await registry.refresh("file") == []
    assert signals == [["inherits", "noted", "own"], ["inherits", "noted"]]
    assert [event.changed_keys for event in seen if isinstance(event, FeatureFlagsChanged)] == [
        ("inherits", "noted", "own"),
        ("inherits", "noted"),
    ]
    await registry.stop()


async def test_a_flag_nested_too_deep_is_a_parse_error_and_never_refuses_its_source() -> None:
    """A 400-evaluator chain expands deeper than 128 levels: that flag is a PARSE_ERROR, the document applies."""
    evaluators: dict[str, Any] = {"chain-0": {"==": [{"var": "tier"}, "gold"]}}
    evaluators |= {f"chain-{i}": {"and": [{"$ref": f"chain-{i - 1}"}]} for i in range(1, 401)}
    deep = {
        "flags": {
            "deep": bool_flag("off", targeting={"if": [{"$ref": "chain-400"}, "on", "off"]}),
            "plain": bool_flag(),
        },
        "$evaluators": evaluators,
    }
    http = DocumentSource("http", {"flags": {"plain": bool_flag("off")}}, deep)
    registry = FlagRegistry([http], FireflyFlagProvider())
    await registry.start()
    assert await registry.refresh("http") == ["deep", "plain"]
    [status] = registry.sources()
    assert (status.status, status.error, status.revision) == ("UP", None, "2")
    with bound_client(registry.provider) as client:
        details = client.get_boolean_details("deep", True, EvaluationContext("u", {"tier": "gold"}))
        assert (details.value, details.error_code) == (True, ErrorCode.PARSE_ERROR)
        assert client.get_boolean_value("plain", False) is True
    await registry.stop()


async def test_an_older_load_finishing_last_never_rolls_the_flags_back() -> None:
    class TwoSpeedSource:
        name = "store"
        fail_fast = False
        refresh_interval = None

        def __init__(self) -> None:
            self.calls = 0
            self.slow = asyncio.Event()

        async def load(self) -> SourceSnapshot:
            self.calls += 1
            if self.calls == 2:
                await self.slow.wait()
                return SourceSnapshot(parse_document({"flags": {"k": bool_flag("off")}}), "old")
            default = "off" if self.calls == 1 else "on"
            return SourceSnapshot(parse_document({"flags": {"k": bool_flag(default)}}), str(self.calls))

        async def close(self) -> None:
            return None

    source = TwoSpeedSource()
    registry = FlagRegistry([source], FireflyFlagProvider())
    await registry.start()
    slow = asyncio.create_task(registry.refresh("store"))
    await wait_until(lambda: source.calls == 2)
    assert await registry.refresh("store") == ["k"]  # the newer load applies first
    source.slow.set()
    assert await slow == []  # the older one is discarded
    assert registry.provider.definition("k") == bool_flag("on")
    await registry.stop()


async def test_a_failing_listener_does_not_stop_the_registry(caplog: pytest.LogCaptureFixture) -> None:
    bus = ApplicationEventBus()

    async def explode(event: FeatureFlagsChanged) -> None:
        raise RuntimeError("listener bug")

    bus.subscribe(FeatureFlagsChanged, explode)
    file = ScriptedSource("file", {"a": True}, {"a": False}, {"a": True})
    registry = FlagRegistry([file], FireflyFlagProvider(), publisher=ApplicationEventPublisher(bus))
    with caplog.at_level(logging.WARNING, logger=_REGISTRY_LOGGER):
        await registry.start()
        assert await registry.refresh("file") == ["a"]
        assert await registry.refresh("file") == ["a"]
    assert [r.getMessage() for r in caplog.records].count("feature_flag_event_listener_failed") == 3
    await registry.stop()


async def test_polling_sources_refresh_until_stopped() -> None:
    file = ScriptedSource("file", {"a": False}, None, {"a": True}, refresh_interval=0.01)
    registry = FlagRegistry([file], FireflyFlagProvider())
    await registry.start()
    polls = [task for task in asyncio.all_tasks() if task.get_name() == "pyfly-feature-flags-file"]
    assert len(polls) == 1
    await wait_until(lambda: registry.provider.definition("a") == bool_flag("on"))
    await registry.stop()
    assert all(task.done() and task.cancelled() for task in polls)  # the poll was cancelled, not left running
    assert not registry.started


async def test_expired_flags_are_logged_once_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    flags = {
        "old": bool_flag(metadata={"expires": "2026-01-31"}),
        "fresh": bool_flag(metadata={"expires": "2027-01-01"}),
    }
    registry = FlagRegistry([StaticSource("config", flags)], FireflyFlagProvider(), today=lambda: dt.date(2026, 10, 1))
    with caplog.at_level(logging.WARNING, logger=_REGISTRY_LOGGER):
        await registry.start()
        await registry.refresh("config")
    expired = [r for r in caplog.records if r.getMessage() == "feature_flag_expired"]
    assert [(r.flag, r.expires) for r in expired] == [("old", "2026-01-31")]  # type: ignore[attr-defined]
    assert registry.expired_keys() == ["old"]
    await registry.stop()


async def test_test_overrides_shadow_every_source_and_are_not_served() -> None:
    registry = FlagRegistry([StaticSource("config", {"a": False, "b": True})], FireflyFlagProvider())
    await registry.start()
    assert registry.set_test_overrides({"a": True}) == ["a"]
    flag = registry.effective_flag("a")
    assert flag is not None and (flag.origin, flag.overrides) == (TEST_OVERRIDES, ("config",))
    assert registry.document()["flags"]["a"]["defaultVariant"] == "on"
    assert registry.document(include_test_overrides=False)["flags"]["a"]["defaultVariant"] == "off"
    assert registry.test_overrides == {"a": True}
    assert registry.clear_test_overrides() == ["a"]
    assert registry.provider.definition("a") == bool_flag("off")
    await registry.stop()


async def test_test_override_changes_are_published_and_stop_drains_them() -> None:
    publisher, seen = recording_publisher()
    registry = FlagRegistry([StaticSource("config", {"a": False})], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    assert registry.set_test_overrides({"a": True}) == ["a"]
    assert seen == [FeatureFlagsChanged(("a",), "startup")]  # scheduled on the loop, not published yet
    await registry.stop()
    assert seen == [FeatureFlagsChanged(("a",), "startup"), FeatureFlagsChanged(("a",), TEST_OVERRIDES)]


async def test_a_removed_key_is_a_changed_key() -> None:
    publisher, seen = recording_publisher()
    file = ScriptedSource("file", {"a": True, "b": True}, {"a": True})
    registry = FlagRegistry([file], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    assert await registry.refresh("file") == ["b"]
    assert seen[-1] == FeatureFlagsChanged(("b",), "file")
    assert registry.provider.definition("b") is None and registry.effective_flag("b") is None
    await registry.stop()


async def test_refresh_all_reloads_every_source_and_returns_every_changed_key() -> None:
    config = ScriptedSource("config", {"a": False, "c": True}, {"a": True, "c": True})
    file = ScriptedSource("file", {"b": False}, {"b": True})
    registry = FlagRegistry([config, file], FireflyFlagProvider())
    await registry.start()
    assert await registry.refresh_all() == ["a", "b"]
    assert (config.loads, file.loads) == (2, 2)
    await registry.stop()


async def test_sources_are_looked_up_by_name() -> None:
    registry = FlagRegistry([StaticSource("config", {"a": True})], FireflyFlagProvider())
    assert registry.has_source("config")
    assert not registry.has_source("http") and not registry.has_source(TEST_OVERRIDES)
    with pytest.raises(KeyError):
        await registry.refresh("http")


async def test_stop_closes_the_sources_and_a_restart_works() -> None:
    source = StaticSource("config", {"a": True})
    registry = FlagRegistry([source], FireflyFlagProvider())
    await registry.start()
    await registry.stop()
    assert source.closed
    await registry.start()
    assert registry.started and registry.effective_flag("a") is not None
    await registry.stop()


def test_source_names_are_unique_and_test_overrides_is_reserved() -> None:
    with pytest.raises(ValueError, match="unique"):
        FlagRegistry([StaticSource("config", {}), StaticSource("config", {})], FireflyFlagProvider())
    with pytest.raises(ValueError, match="reserved"):
        FlagRegistry([StaticSource(TEST_OVERRIDES, {})], FireflyFlagProvider())


# -- events held until the context is refreshed, and the lifecycle phase ------------------------------------------


async def test_held_events_wait_for_the_context_refresh_and_are_released_once_in_order() -> None:
    """The context wires the ``@app_event_listener`` methods after it started the lifecycle beans: the events of the
    boot composition and of the refreshes before the listeners exist wait for ``ContextRefreshedEvent``."""
    publisher, seen = recording_publisher()
    file = ScriptedSource("file", {"a": False}, {"a": True}, {"a": False})
    registry = FlagRegistry([file], FireflyFlagProvider(), publisher=publisher, hold_events=True)
    await registry.start()
    assert await registry.refresh("file") == ["a"]
    assert registry.set_test_overrides({"b": True}) == ["b"]
    await wait_until(lambda: not registry._pending)  # noqa: SLF001 — the override's publish ran, and was held
    assert seen == []
    await registry.on_context_refreshed(ContextRefreshedEvent())
    assert seen == [
        FeatureFlagsChanged(("a",), "startup"),
        FeatureFlagsChanged(("a",), "file"),
        FeatureFlagsChanged(("b",), TEST_OVERRIDES),
    ]
    await registry.on_context_refreshed(ContextRefreshedEvent())  # released once: nothing is published twice
    assert len(seen) == 3
    assert await registry.refresh("file") == ["a"]  # from now on, published at once
    assert seen[-1] == FeatureFlagsChanged(("a",), "file") and len(seen) == 4
    await registry.stop()


async def test_a_stop_before_the_context_refresh_drops_the_held_events() -> None:
    publisher, seen = recording_publisher()
    config = ScriptedSource("config", {"a": True}, {"a": False})
    registry = FlagRegistry([config], FireflyFlagProvider(), publisher=publisher, hold_events=True)
    await registry.start()
    await registry.stop()
    await registry.on_context_refreshed(ContextRefreshedEvent())  # a refresh after the stop finds nothing held
    assert seen == []
    await registry.start()  # started again (the source now answers a=false), it holds until the next refresh
    assert seen == []
    await registry.on_context_refreshed(ContextRefreshedEvent())
    assert seen == [FeatureFlagsChanged(("a",), "startup")]
    await registry.stop()


async def test_a_registry_that_does_not_hold_publishes_at_once() -> None:
    publisher, seen = recording_publisher()
    registry = FlagRegistry([StaticSource("config", {"a": True})], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    assert seen == [FeatureFlagsChanged(("a",), "startup")]
    await registry.on_context_refreshed(ContextRefreshedEvent())
    assert seen == [FeatureFlagsChanged(("a",), "startup")]
    await registry.stop()


def test_the_registry_starts_in_the_feature_flags_phase() -> None:
    """After the datasource, the migrations and the schema (the store needs them), before the application's beans."""
    assert lifecycle_phase(FlagRegistry([], FireflyFlagProvider())) == FEATURE_FLAGS_PHASE
    assert MIGRATION_PHASE < SCHEMA_PHASE < FEATURE_FLAGS_PHASE < DEFAULT_PHASE
