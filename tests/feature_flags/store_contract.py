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
"""The behavior every FlagStore shares. Subclass with an async ``store`` fixture."""

from __future__ import annotations

import pytest

from pyfly.feature_flags.store.ports import FlagConflictError, FlagNotStoredError, FlagStore
from tests.feature_flags.support import bool_flag


class FlagStoreContract:
    async def test_an_empty_store(self, store: FlagStore) -> None:
        assert isinstance(store, FlagStore)
        assert await store.all() == {}
        assert await store.get("a") is None
        assert await store.revision() == 0
        assert await store.history("a") == []

    async def test_put_inserts_then_updates_with_versions_and_history(self, store: FlagStore) -> None:
        first = await store.put("a", bool_flag("on"), actor="ana")
        assert (first.key, first.action, first.definition, first.previous, first.actor) == (
            "a",
            "put",
            bool_flag("on"),
            None,
            "ana",
        )
        second = await store.put("a", bool_flag("off"), actor="bob")
        assert second.previous == bool_flag("on") and second.id > first.id
        stored = await store.get("a")
        assert stored is not None and (stored.version, stored.definition, stored.updated_by) == (
            2,
            bool_flag("off"),
            "bob",
        )
        assert stored.updated_at.tzinfo is not None and stored.updated_at.utcoffset().total_seconds() == 0
        assert await store.revision() == second.id
        assert [change.id for change in await store.history("a")] == [second.id, first.id]

    async def test_delete_records_the_previous_definition(self, store: FlagStore) -> None:
        await store.put("a", bool_flag(), actor=None)
        change = await store.delete("a", actor="ops")
        assert (change.action, change.definition, change.previous, change.actor) == ("delete", None, bool_flag(), "ops")
        assert await store.get("a") is None and await store.all() == {}
        assert await store.revision() == change.id
        with pytest.raises(FlagNotStoredError):
            await store.delete("a", actor="ops")

    async def test_expected_version_guards_every_write(self, store: FlagStore) -> None:
        await store.put("a", bool_flag(), actor=None, expected_version=0)
        with pytest.raises(FlagConflictError) as raised:
            await store.put("a", bool_flag("off"), actor=None, expected_version=0)
        assert (raised.value.expected, raised.value.actual) == (0, 1)
        with pytest.raises(FlagConflictError):
            await store.put("a", bool_flag("off"), actor=None, expected_version=5)
        with pytest.raises(FlagConflictError):
            await store.delete("a", actor=None, expected_version=2)
        stored = await store.get("a")
        assert stored is not None and stored.version == 1
        assert len(await store.history("a")) == 1
        await store.put("a", bool_flag("off"), actor=None, expected_version=1)
        await store.delete("a", actor=None, expected_version=2)

    async def test_history_is_per_key_and_limited(self, store: FlagStore) -> None:
        for default in ("on", "off", "on"):
            await store.put("a", bool_flag(default), actor=None)
        await store.put("b", bool_flag(), actor=None)
        history = await store.history("a", limit=2)
        assert [change.definition for change in history] == [bool_flag("on"), bool_flag("off")]
        assert {change.key for change in await store.history("a")} == {"a"}

    async def test_all_returns_independent_copies(self, store: FlagStore) -> None:
        await store.put("a", bool_flag(metadata={"owner": "web"}), actor=None)
        rows = await store.all()
        rows["a"].definition["metadata"]["owner"] = "changed"
        stored = await store.get("a")
        assert stored is not None and stored.definition["metadata"]["owner"] == "web"

    async def test_non_ascii_definitions_round_trip(self, store: FlagStore) -> None:
        definition = bool_flag(metadata={"description": "Café — 東京 🚀"})
        await store.put("a", definition, actor="José")
        stored = await store.get("a")
        assert stored is not None and stored.definition == definition and stored.updated_by == "José"

    async def test_put_persists_the_canonical_definition_in_row_and_change(self, store: FlagStore) -> None:
        raw = bool_flag(targeting=[], metadata=[])
        change = await store.put("a", raw, actor=None)
        canonical = bool_flag(targeting={}, metadata={})
        assert raw == bool_flag(targeting=[], metadata=[])
        assert change.definition == canonical
        stored = await store.get("a")
        assert stored is not None and stored.definition == canonical
        assert (await store.history("a"))[0].definition == canonical
        deleted = await store.delete("a", actor=None)
        assert deleted.previous == canonical

    async def test_returned_change_does_not_mutate_store_history(self, store: FlagStore) -> None:
        raw = bool_flag(metadata={"owner": "web"})
        change = await store.put("a", raw, actor=None)
        raw["metadata"]["owner"] = "input-change"
        assert change.definition is not None
        change.definition["metadata"]["owner"] = "output-change"
        history = await store.history("a")
        assert history[0].definition == bool_flag(metadata={"owner": "web"})
        stored = await store.get("a")
        assert stored is not None and stored.definition == bool_flag(metadata={"owner": "web"})
