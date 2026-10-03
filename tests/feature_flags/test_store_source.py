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
"""The store layer: revision polling, writes that refresh the writer's composition, propagation (spec 4.6)."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import infrastructure_unit
from pyfly.feature_flags.definitions import FlagDefinitionError
from pyfly.feature_flags.events import FeatureFlagsChanged, FeatureFlagUpdated
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.sources.store import STORE_SOURCE, StoreFlagSource
from pyfly.feature_flags.store.memory import MemoryFlagStore
from pyfly.feature_flags.store.ports import FlagStore, StoredFlag
from pyfly.feature_flags.store.sqlalchemy import SqlAlchemyFlagStore
from pyfly.feature_flags.store.writer import FlagStoreWriter
from tests.feature_flags.support import StaticSource, bool_flag, recording_publisher
from tests.support.backend_matrix import RelationalBackend


async def test_the_source_reloads_only_when_the_revision_moved() -> None:
    store = MemoryFlagStore()
    source = StoreFlagSource(store, refresh_interval=0.5)
    assert (source.name, source.fail_fast, source.refresh_interval) == (STORE_SOURCE, False, 0.5)
    first = await source.load()
    assert first is not None and first.document.flags == {} and first.revision == "0"
    assert await source.load() is None
    await store.put("a", bool_flag(), actor=None)
    changed = await source.load()
    assert changed is not None and changed.document.flags == {"a": bool_flag()} and changed.revision == "1"


async def test_an_invalid_row_rejects_the_whole_store_document_until_it_is_fixed() -> None:
    store = MemoryFlagStore()
    await store.put("good", bool_flag(), actor=None)
    await store.put("bad", bool_flag(), actor="another process")
    original_all = store.all

    async def corrupted_all() -> dict[str, StoredFlag]:
        rows = await original_all()
        rows["bad"] = replace(rows["bad"], definition={"state": "MAYBE", "variants": {"a": 1}})
        return rows

    store.all = corrupted_all  # type: ignore[method-assign]
    source = StoreFlagSource(store)
    with pytest.raises(FlagDefinitionError, match="state must be ENABLED or DISABLED"):
        await source.load()
    with pytest.raises(FlagDefinitionError):
        await source.load()  # the revision is not remembered: the next poll validates again
    store.all = original_all  # type: ignore[method-assign]
    await store.put("bad", bool_flag("off"), actor=None)
    assert (await source.load()) is not None


async def _registry(store: FlagStore, publisher: ApplicationEventPublisher | None = None) -> FlagRegistry:
    sources = [StaticSource("config", {"a": False}), StoreFlagSource(store)]
    registry = FlagRegistry(sources, FireflyFlagProvider(), publisher=publisher)
    await registry.start()
    return registry


async def test_a_write_refreshes_the_writer_s_composition_and_publishes_feature_flag_updated() -> None:
    store = MemoryFlagStore()
    publisher, seen = recording_publisher()
    registry = await _registry(store, publisher)
    writer = FlagStoreWriter(store, registry, publisher=publisher)
    change = await writer.put("a", bool_flag("on"), actor="ana")
    flag = registry.effective_flag("a")
    assert flag is not None and (flag.origin, flag.overrides) == ("store", ("config",))
    assert registry.provider.definition("a") == bool_flag("on")
    assert FeatureFlagsChanged(("a",), "store") in seen
    assert seen[-1] == FeatureFlagUpdated("a", "put", "ana", None, bool_flag("on"))
    assert change.definition == bool_flag("on")
    await writer.delete("a", actor="ana")
    assert registry.effective_flag("a").origin == "config"  # type: ignore[union-attr]
    assert seen[-1] == FeatureFlagUpdated("a", "delete", "ana", bool_flag("on"), None)
    await registry.stop()


async def test_a_write_is_validated_before_it_reaches_the_store() -> None:
    store = MemoryFlagStore()
    registry = await _registry(store)
    writer = FlagStoreWriter(store, registry)
    with pytest.raises(FlagDefinitionError, match="defaultVariant is not a variant"):
        await writer.put("a", {**bool_flag(), "defaultVariant": "maybe"}, actor=None)
    assert await store.revision() == 0
    await registry.stop()


async def test_a_failing_listener_never_turns_a_committed_write_into_an_error(caplog: pytest.LogCaptureFixture) -> None:
    bus = ApplicationEventBus()

    async def explode(event: FeatureFlagUpdated) -> None:
        raise RuntimeError("listener bug")

    bus.subscribe(FeatureFlagUpdated, explode)
    store = MemoryFlagStore()
    registry = await _registry(store)
    writer = FlagStoreWriter(store, registry, publisher=ApplicationEventPublisher(bus))
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.store.writer"):
        change = await writer.put("a", bool_flag(), actor=None)
    assert change.id == 1 and (await store.get("a")) is not None
    assert any(record.getMessage() == "feature_flag_event_listener_failed" for record in caplog.records)
    await registry.stop()


async def test_a_second_process_sees_a_write_on_its_next_refresh(tmp_path: Path) -> None:
    """Two registries sharing one database: the writer sees its change at once, the other on its next poll."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'flags.db'}"
    engines = [create_async_engine(url), create_async_engine(url)]
    stores = [SqlAlchemyFlagStore(engine) for engine in engines]
    for store in stores:
        await store.start()
    writer_side = FlagRegistry([StoreFlagSource(stores[0])], FireflyFlagProvider())
    reader_side = FlagRegistry([StoreFlagSource(stores[1])], FireflyFlagProvider())
    await writer_side.start()
    await reader_side.start()
    await FlagStoreWriter(stores[0], writer_side).put("shared", bool_flag("on"), actor="ops")
    assert writer_side.effective_flag("shared") is not None
    assert reader_side.effective_flag("shared") is None
    assert await reader_side.refresh(STORE_SOURCE) == ["shared"]
    assert reader_side.provider.definition("shared") == bool_flag("on")
    for registry in (writer_side, reader_side):
        await registry.stop()
    for engine in engines:
        await engine.dispose()


async def test_writer_canonicalizes_the_definition_in_row_history_and_event() -> None:
    store = MemoryFlagStore()
    publisher, seen = recording_publisher()
    registry = await _registry(store)
    writer = FlagStoreWriter(store, registry, publisher=publisher)
    change = await writer.put("a", bool_flag(targeting=[], metadata=[], ignored=True), actor=None)
    expected = bool_flag(targeting={}, metadata={}, ignored=True)
    assert change.definition == expected
    assert (await store.get("a")).definition == expected
    assert (await store.history("a"))[0].definition == expected
    assert seen[-1] == FeatureFlagUpdated("a", "put", None, None, expected)
    await registry.stop()


@pytest.mark.parametrize("refused", [False, True])
async def test_writer_distinguishes_unchanged_composition_from_refused_refresh(
    refused: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = MemoryFlagStore()
    publisher, seen = recording_publisher()
    registry = await _registry(store)
    writer = FlagStoreWriter(store, registry, publisher=publisher)
    await writer.put("a", bool_flag("off"), actor=None)
    if refused:

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("provider refused")

        monkeypatch.setattr(registry.provider, "update", refuse)
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.store.writer"):
        change = await writer.put("a", bool_flag("on" if refused else "off"), actor="ops")
    warnings = [r for r in caplog.records if r.getMessage() == "feature_flag_store_refresh_failed"]
    assert bool(warnings) is refused
    assert registry.provider.definition("a") == bool_flag("off")
    assert seen[-1] == FeatureFlagUpdated("a", "put", "ops", bool_flag("off"), change.definition)
    await registry.stop()


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("action", ["put", "delete"])
async def test_writer_waits_for_its_sql_transaction_commit(
    relational_backend: RelationalBackend, rollback: bool, action: str
) -> None:
    engine = relational_backend.create_engine()
    store = SqlAlchemyFlagStore(engine)
    await store.start()
    await store.put("a", bool_flag("off"), actor=None)
    publisher, seen = recording_publisher()
    registry = await _registry(store, publisher)
    seen.clear()
    writer = FlagStoreWriter(store, registry, publisher=publisher)

    class Abort(Exception):
        pass

    try:
        async with infrastructure_unit(engine):
            if action == "put":
                await writer.put("a", bool_flag("on"), actor="ops")
            else:
                await writer.delete("a", actor="ops")
            assert registry.provider.definition("a") == bool_flag("off")
            assert seen == []
            if rollback:
                raise Abort
    except Abort:
        pass
    if rollback:
        assert registry.provider.definition("a") == bool_flag("off")
        assert seen == []
        assert (await store.get("a")).definition == bool_flag("off")
        assert len(await store.history("a")) == 1
    else:
        expected = bool_flag("on") if action == "put" else None
        assert seen[-1] == FeatureFlagUpdated("a", action, "ops", bool_flag("off"), expected)
        assert registry.effective_flag("a").origin == ("store" if action == "put" else "config")
        assert registry.provider.definition("a") == (expected or bool_flag("off"))
        assert len(await store.history("a")) == 2
    await registry.stop()


@pytest.mark.parametrize("memory", [False, True])
async def test_an_unrelated_transaction_does_not_delay_a_committed_write(tmp_path: Path, memory: bool) -> None:
    other_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'flags.db'}")
    store = MemoryFlagStore() if memory else SqlAlchemyFlagStore(engine)
    await store.start()
    publisher, seen = recording_publisher()
    registry = await _registry(store)
    writer = FlagStoreWriter(store, registry, publisher=publisher)

    class Abort(Exception):
        pass

    with pytest.raises(Abort):
        async with infrastructure_unit(
            SqlAlchemyTransactionManager(sessionmaker=async_sessionmaker(other_engine), name="other")
        ):
            await writer.put("a", bool_flag(), actor=None)
            assert registry.provider.definition("a") == bool_flag()
            assert seen == [FeatureFlagUpdated("a", "put", None, None, bool_flag())]
            raise Abort
    assert (await store.get("a")).definition == bool_flag()
    await registry.stop()
    await engine.dispose()
    await other_engine.dispose()


async def test_a_rejected_store_document_keeps_the_entire_last_good_layer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = MemoryFlagStore()
    await store.put("a", bool_flag("off"), actor=None)
    registry = await _registry(store)
    original_all = store.all

    async def corrupt() -> dict[str, StoredFlag]:
        rows = await original_all()
        rows["broken"] = replace(rows["a"], key="broken", definition={"state": "MAYBE"})
        return rows

    monkeypatch.setattr(store, "all", corrupt)
    publisher, seen = recording_publisher()
    with caplog.at_level(logging.WARNING):
        await FlagStoreWriter(store, registry, publisher=publisher).put("a", bool_flag("on"), actor=None)
    assert registry.provider.definition("a") == bool_flag("off")
    assert registry.effective_flag("broken") is None
    assert next(s for s in registry.sources() if s.name == STORE_SOURCE).status == "STALE"
    assert any(r.getMessage() == "feature_flag_store_refresh_failed" for r in caplog.records)
    assert seen[-1] == FeatureFlagUpdated("a", "put", None, bool_flag("off"), bool_flag("on"))
    monkeypatch.setattr(store, "all", original_all)
    assert await registry.refresh(STORE_SOURCE) == ["a"]
    assert registry.provider.definition("a") == bool_flag("on")
    await registry.stop()


@pytest.mark.parametrize("release_child", [False, True])
async def test_a_caller_savepoint_rollback_never_publishes_the_rolled_back_write(
    relational_backend: RelationalBackend,
    release_child: bool,
) -> None:
    from pyfly.data.transaction import Propagation, TransactionTemplate

    engine = relational_backend.create_engine()
    store = SqlAlchemyFlagStore(engine)
    await store.start()
    publisher, seen = recording_publisher()
    registry = await _registry(store, publisher)
    seen.clear()
    writer = FlagStoreWriter(store, registry, publisher=publisher)

    class Abort(Exception):
        pass

    async with infrastructure_unit(engine):
        await writer.put("kept", bool_flag(), actor="ops")
        with pytest.raises(Abort):
            async with TransactionTemplate(engine, propagation=Propagation.NESTED).transaction():
                if release_child:
                    async with TransactionTemplate(engine, propagation=Propagation.NESTED).transaction():
                        await writer.put("discarded", bool_flag(), actor="ops")
                else:
                    await writer.put("discarded", bool_flag(), actor="ops")
                raise Abort
        await writer.put("also-kept", bool_flag(), actor="ops")
        assert seen == []
    updated = [e.key for e in seen if isinstance(e, FeatureFlagUpdated)]
    assert updated == ["kept", "also-kept"]
    assert registry.effective_flag("discarded") is None
    assert await store.get("discarded") is None
    assert await store.history("discarded") == []
    await registry.stop()


async def test_rolled_back_savepoint_callback_is_pruned_when_the_same_write_commits(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pyfly.data.transaction import Propagation, TransactionTemplate

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reuse.db'}")
    store = SqlAlchemyFlagStore(engine, clock=lambda: datetime(2026, 10, 3, tzinfo=UTC))
    await store.start()
    publisher, seen = recording_publisher()
    registry = await _registry(store)
    writer = FlagStoreWriter(store, registry, publisher=publisher)

    class Abort(Exception):
        pass

    async with infrastructure_unit(engine):
        await store.put("anchor", bool_flag(), actor=None)
        with pytest.raises(Abort):
            async with TransactionTemplate(engine, propagation=Propagation.NESTED).transaction():
                abandoned = await writer.put("a", bool_flag(), actor=None)
                raise Abort
        committed = await writer.put("a", bool_flag(), actor=None)
        assert abandoned == committed  # SQLite reused the rolled-back ID; row equality cannot identify ownership.
    assert seen == [FeatureFlagUpdated("a", "put", None, None, bool_flag())]
    await registry.stop()
    await engine.dispose()
