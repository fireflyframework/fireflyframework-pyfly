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
from collections.abc import Mapping
from typing import Any

import pytest
from openfeature.evaluation_context import EvaluationContext

from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher
from pyfly.feature_flags.definitions import FlagDefinitionError, FlagDocument, parse_document
from pyfly.feature_flags.events import FeatureFlagsChanged
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import TEST_OVERRIDES, FlagRegistry
from pyfly.feature_flags.sources import FlagSourceError, SourceSnapshot
from tests.feature_flags.support import (
    ScriptedSource,
    StaticSource,
    bool_flag,
    recording_publisher,
    wait_until,
)

_REGISTRY_LOGGER = "pyfly.feature_flags.registry"


class DocumentSource:
    """A source answering, load after load, the next scripted result: a whole flagd document (validated here), a
    ready ``FlagDocument`` (taken as it is, as a source that skips ``parse_document`` would) or ``None`` (unchanged).
    When the script runs out it answers ``None``."""

    fail_fast = False
    refresh_interval: float | None = None

    def __init__(self, name: str, *results: Mapping[str, Any] | FlagDocument | None) -> None:
        self.name = name
        self._results = list(results)
        self.loads = 0

    async def load(self) -> SourceSnapshot | None:
        self.loads += 1
        result = self._results.pop(0) if self._results else None
        if result is None:
            return None
        document = result if isinstance(result, FlagDocument) else parse_document(result)
        return SourceSnapshot(document, str(self.loads))

    async def close(self) -> None:
        return None


def _refused_document(key: str) -> FlagDocument:
    """A document FlagdCore refuses (``state`` is not ENABLED/DISABLED); ``parse_document`` would have rejected it."""
    return FlagDocument(flags={key: {"state": "ARCHIVED", "variants": {"on": True, "off": False}}})


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
                "nested": bool_flag("off", targeting={"if": [{"$ref": "insider"}, "on", "off"]}),
                "other": bool_flag("off", targeting={"if": [{"$ref": "is-staff"}, "on", "off"]}),
                "static": bool_flag(),
            },
            "$evaluators": {
                # "insider" sorts before the evaluators it $refs, so flagd resolves them inside it.
                "insider": {"or": [{"$ref": "is-staff"}, {"$ref": "is-beta"}]},
                "is-beta": {"in": [{"var": "role"}, beta_roles]},
                "is-staff": {"==": [{"var": "role"}, "staff"]},
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
    registry = FlagRegistry([http], FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    composition, document = registry.composition(), registry.provider.document
    with caplog.at_level(logging.ERROR, logger=_REGISTRY_LOGGER):
        assert await registry.refresh("http") == []
    assert [r.getMessage() for r in caplog.records] == ["feature_flag_provider_update_failed"]
    assert registry.composition() is composition and registry.provider.document == document
    assert registry.layers("a") == [("http", bool_flag("off"))]  # the source keeps its last good document
    [status] = registry.sources()
    assert status.status == "STALE" and status.revision == "1"
    assert status.error is not None and status.error.startswith("ParseError: ")
    assert await registry.refresh("http") == []  # "unchanged" is still the refused document
    assert registry.sources()[0].status == "STALE"
    assert seen == [FeatureFlagsChanged(("a",), "startup")]
    assert await registry.refresh("http") == ["a"]  # a new document applies
    assert registry.sources()[0].status == "UP" and registry.provider.definition("a") == bool_flag("on")
    await registry.stop()


async def test_a_refused_boot_composition_is_blamed_on_no_source(caplog: pytest.LogCaptureFixture) -> None:
    """Which layer FlagdCore objects to is unknown at boot: no source is marked, and a later refusal (the refused
    layer is still composed) is not blamed on the source that refreshed."""
    file = DocumentSource("file", {"flags": {"a": bool_flag()}}, {"flags": {"a": bool_flag("off")}})
    registry = FlagRegistry([DocumentSource("config", _refused_document("bad")), file], FireflyFlagProvider())
    with caplog.at_level(logging.ERROR, logger=_REGISTRY_LOGGER):
        await registry.start()
        assert await registry.refresh("file") == []
    assert [r.getMessage() for r in caplog.records] == ["feature_flag_provider_update_failed"] * 2
    assert registry.composition().flags == {} and registry.provider.document == {"flags": {}}
    assert [(status.status, status.revision) for status in registry.sources()] == [("UP", "1"), ("UP", "2")]
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
