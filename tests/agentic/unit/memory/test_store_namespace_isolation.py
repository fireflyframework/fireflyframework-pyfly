"""Lightweight stores must not alias distinct namespace identities or adopt ambiguous legacy data."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pyfly.agentic.exceptions import DatabaseStoreError
from pyfly.agentic.memory.store import FileStore, SQLiteStore
from pyfly.agentic.memory.types import MemoryEntry


@pytest.mark.parametrize("namespaces", [("sales/report", "sales_report"), ("sales_report", "sales\\report")])
def test_file_namespace_separator_encoding_cannot_alias_a_safe_name(
    tmp_path: Path, namespaces: tuple[str, str]
) -> None:
    store = FileStore(tmp_path)
    first, second = namespaces
    store.save(first, MemoryEntry(entry_id="same", key="value", content="first"))
    store.save(second, MemoryEntry(entry_id="same", key="value", content="second"))
    assert store.load_by_key(first, "value").content == "first"
    assert store.load_by_key(second, "value").content == "second"
    store.delete(first, "same")
    assert store.load(first) == []
    assert store.load_by_key(second, "value").content == "second"
    store.clear(first)
    assert len(store.load(second)) == 1


def test_a_literal_namespace_cannot_impersonate_an_encoded_filename(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    store.save("sales/report", MemoryEntry(entry_id="id", content="original"))
    encoded_name = store._path("sales/report").stem
    store.save(encoded_name, MemoryEntry(entry_id="id", content="literal-name"))
    assert store.load("sales/report")[0].content == "original"
    assert store.load(encoded_name)[0].content == "literal-name"


@pytest.mark.parametrize("namespace", ["sales/report", "sales\\report", "sales_report"])
@pytest.mark.parametrize("operation", ["load", "save", "delete", "clear"])
def test_ambiguous_legacy_file_requires_explicit_namespace_migration(
    tmp_path: Path, namespace: str, operation: str
) -> None:
    original = b'{"old": {"entry_id": "old", "content": "retained"}}'
    legacy = tmp_path / "sales_report.json"
    legacy.write_bytes(original)
    store = FileStore(tmp_path)
    arguments = {"load": (), "save": (MemoryEntry(entry_id="new"),), "delete": ("old",), "clear": ()}
    with pytest.raises(DatabaseStoreError, match="migration"):
        getattr(store, operation)(namespace, *arguments[operation])
    assert legacy.read_bytes() == original
    assert list(tmp_path.iterdir()) == [legacy]


def test_unambiguous_legacy_files_keep_their_location_and_entry_payload(tmp_path: Path) -> None:
    legacy = tmp_path / "ordinary.json"
    legacy.write_text('{"old": {"entry_id": "old", "key": "answer", "content": 42}}', encoding="utf-8")
    store = FileStore(tmp_path)
    assert store.load_by_key("ordinary", "answer").content == 42
    store.save("ordinary", MemoryEntry(entry_id="new", content="new"))
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    assert set(payload) == {"old", "new"}
    assert payload["old"]["content"] == 42
    assert store._path("ordinary") == legacy


def test_confirmed_safe_legacy_namespace_can_be_read_without_relocating_its_payload(tmp_path: Path) -> None:
    legacy = tmp_path / "sales_report.json"
    original = '{"old": {"entry_id": "old", "content": "retained"}}'
    legacy.write_text(original, encoding="utf-8")
    legacy.with_name(legacy.name + ".namespace").write_text(json.dumps("sales_report"), encoding="utf-8")
    store = FileStore(tmp_path)
    assert store.load("sales_report")[0].content == "retained"
    assert legacy.read_text(encoding="utf-8") == original
    store.save("sales/report", MemoryEntry(entry_id="new", content="separate"))
    assert store.load("sales/report")[0].content == "separate"
    assert store.load("sales_report")[0].content == "retained"


def test_file_namespace_cannot_follow_a_symlink_outside_its_store(tmp_path: Path) -> None:
    directory = tmp_path / "store"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (directory / "safe.json").symlink_to(outside)
    with pytest.raises(ValueError, match="(traversal|symbolic|symlink)"):
        FileStore(directory).save("safe", MemoryEntry())
    assert outside.read_text(encoding="utf-8") == "{}"


def test_parent_segments_in_a_namespace_stay_inside_the_store(tmp_path: Path) -> None:
    directory = tmp_path / "store"
    store = FileStore(directory)
    store.save("../outside", MemoryEntry(content="inside"))
    assert store.load("../outside")[0].content == "inside"
    assert not (tmp_path / "outside.json").exists()
    assert all(path.is_relative_to(directory) for path in directory.iterdir())


async def test_concurrent_file_async_saves_do_not_lose_entries(tmp_path: Path) -> None:
    stores = [FileStore(tmp_path), FileStore(tmp_path)]
    await asyncio.gather(
        *(stores[i % 2].async_save("shared", MemoryEntry(entry_id=str(i), content=i)) for i in range(32))
    )
    assert {entry.content for entry in await stores[0].async_load("shared")} == set(range(32))


def test_failed_atomic_file_replacement_keeps_original_payload_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileStore(tmp_path)
    store.save("ordinary", MemoryEntry(entry_id="old", content="retained"))
    original = (tmp_path / "ordinary.json").read_bytes()
    replace = Path.replace

    def cannot_replace(path: Path, target: Path) -> Path:
        if target.name == "ordinary.json":
            raise OSError("replacement failed")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", cannot_replace)
    with pytest.raises(OSError, match="replacement failed"):
        store.save("ordinary", MemoryEntry(entry_id="new", content="not-written"))
    assert (tmp_path / "ordinary.json").read_bytes() == original
    assert not list(tmp_path.glob(".pyfly-memory-*.tmp"))


@pytest.mark.parametrize("marker", ['"another-namespace"', "{}", "invalid-json"])
def test_file_marker_mismatch_or_corruption_fails_closed(tmp_path: Path, marker: str) -> None:
    payload = tmp_path / "ordinary.json"
    payload.write_text('{"old": {"entry_id": "old", "content": "retained"}}', encoding="utf-8")
    payload.with_name(payload.name + ".namespace").write_text(marker, encoding="utf-8")
    original = payload.read_bytes()
    with pytest.raises(DatabaseStoreError, match="migration"):
        FileStore(tmp_path).clear("ordinary")
    assert payload.read_bytes() == original


def test_sqlite_same_id_in_two_namespaces_does_not_move_or_delete_foreign_data(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory.sqlite")
    store.save("one", MemoryEntry(entry_id="same", content="first"))
    store.save("two", MemoryEntry(entry_id="same", content="other"))
    store.save("one", MemoryEntry(entry_id="same", content="updated"))
    assert store.load("one")[0].content == "updated"
    assert store.load("two")[0].content == "other"
    store.delete("one", "same")
    assert store.load("one") == []
    assert store.load("two")[0].content == "other"
    store.clear("one")
    assert store.load("two")[0].content == "other"


def test_sqlite_replacement_keeps_its_existing_append_order(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory.sqlite")
    store.save("one", MemoryEntry(entry_id="first", content="first"))
    store.save("one", MemoryEntry(entry_id="second", content="second"))
    store.save("one", MemoryEntry(entry_id="first", content="updated"))
    assert [entry.content for entry in store.load("one")] == ["second", "updated"]


def test_sqlite_global_primary_key_requires_explicit_migration_without_rewriting_data(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE firefly_memory (entry_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, content TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO firefly_memory VALUES (?, ?, ?)", ("old", "one", '{"entry_id":"old","content":42}')
        )
    original = path.read_bytes()
    with pytest.raises(DatabaseStoreError, match="migration"):
        SQLiteStore(path)
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT namespace, content FROM firefly_memory").fetchone() == (
            "one",
            '{"entry_id":"old","content":42}',
        )


def test_sqlite_named_global_unique_index_also_requires_migration(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE firefly_memory (entry_id TEXT NOT NULL, namespace TEXT NOT NULL, "
            "content TEXT NOT NULL, PRIMARY KEY(namespace, entry_id))"
        )
        connection.execute('CREATE UNIQUE INDEX "legacy""id" ON firefly_memory(entry_id)')
    original = path.read_bytes()
    with pytest.raises(DatabaseStoreError, match="migration"):
        SQLiteStore(path)
    assert path.read_bytes() == original
