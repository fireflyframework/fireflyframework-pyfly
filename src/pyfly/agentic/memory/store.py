# Copyright 2026 Firefly Software Foundation
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

"""Pluggable memory store backends.

:class:`MemoryStore` is the protocol that all backends must satisfy.
The framework ships with :class:`InMemoryStore` (default, dict-backed),
:class:`FileStore` (JSON file persistence), and :class:`SQLiteStore`
(stdlib SQLite). All three are stdlib-only — no optional dependency
required.

Backends that require external services (PostgreSQL, MongoDB) live in
:mod:`pyfly.agentic.memory.database_store` behind optional
dependency groups.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol, cast, runtime_checkable

from pyfly.agentic.exceptions import DatabaseConnectionError, DatabaseStoreError
from pyfly.agentic.memory.types import MemoryEntry

logger = logging.getLogger(__name__)


@runtime_checkable
class MemoryStore(Protocol):
    """Protocol for memory persistence backends.

    Implementations must support namespace-scoped CRUD operations so
    that different conversations, agents, or pipelines can maintain
    independent storage.
    """

    def save(self, namespace: str, entry: MemoryEntry) -> None:
        """Persist a single :class:`MemoryEntry` under *namespace*."""
        ...

    def load(self, namespace: str) -> list[MemoryEntry]:
        """Return all entries stored under *namespace*."""
        ...

    def load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        """Return the entry matching *key*, or *None*."""
        ...

    def delete(self, namespace: str, entry_id: str) -> None:
        """Remove a single entry by ID."""
        ...

    def clear(self, namespace: str) -> None:
        """Remove all entries in *namespace*."""
        ...


class InMemoryStore:
    """Dict-backed in-memory store.  Fast but non-persistent.

    Suitable for testing, short-lived sessions, and as the default backend.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, MemoryEntry]] = defaultdict(dict)
        self._lock = threading.Lock()

    def save(self, namespace: str, entry: MemoryEntry) -> None:
        with self._lock:
            self._data[namespace][entry.entry_id] = entry

    def load(self, namespace: str) -> list[MemoryEntry]:
        with self._lock:
            entries = list(self._data.get(namespace, {}).values())
        return [e for e in entries if not e.is_expired]

    def load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        with self._lock:
            for entry in self._data.get(namespace, {}).values():
                if entry.key == key and not entry.is_expired:
                    return entry
        return None

    def delete(self, namespace: str, entry_id: str) -> None:
        with self._lock:
            self._data.get(namespace, {}).pop(entry_id, None)

    def clear(self, namespace: str) -> None:
        with self._lock:
            self._data.pop(namespace, None)

    @property
    def namespaces(self) -> list[str]:
        """Return all namespaces that have stored entries."""
        return list(self._data.keys())


class FileStore:
    """JSON file-backed store for lightweight persistence.

    Each namespace has a JSON file under *base_dir* and an ownership marker beside it. Safe names retain
    their file paths; separators and reserved names use reversible UTF-8 hex encoding. Ambiguous legacy
    filenames require explicit migration before access. Writes replace files atomically, and one process
    serializes read/modify/write operations across its FileStore instances.

    Parameters:
        base_dir: Directory where namespace JSON files are written.
    """

    _ENCODED_PREFIX = "__pyfly_namespace_v1__"
    _lock = threading.RLock()

    def __init__(self, base_dir: str | Path = ".firefly_memory") -> None:
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _checked_path(self, name: str) -> Path:
        candidate = self._base_dir / name
        if candidate.is_symlink():
            raise ValueError(f"File memory paths cannot be symbolic links: {candidate}")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._base_dir):
            raise ValueError(f"Path traversal detected in memory file: {name!r}")
        return resolved

    def _owner(self, path: Path) -> str | None:
        marker = self._checked_path(path.name + ".namespace")
        if not marker.exists():
            return None
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise DatabaseStoreError(f"File memory ownership marker {marker} requires explicit migration") from exc
        if not isinstance(owner, str):
            raise DatabaseStoreError(f"File memory ownership marker {marker} requires explicit migration")
        return owner

    @staticmethod
    def _migration(path: Path) -> DatabaseStoreError:
        return DatabaseStoreError(
            f"File memory {path} requires explicit namespace migration: confirm its original namespace, "
            "preserve its payload, and provision the matching <filename>.namespace JSON string marker"
        )

    def _path(self, namespace: str) -> Path:
        encoded = "/" in namespace or "\\" in namespace or namespace.startswith(self._ENCODED_PREFIX)
        name = self._ENCODED_PREFIX + namespace.encode("utf-8").hex() if encoded else namespace
        path = self._checked_path(f"{name}.json")
        if encoded:
            legacy_name = namespace.replace("/", "_").replace("\\", "_")
            legacy = self._checked_path(f"{legacy_name}.json")
            if legacy != path and legacy.exists() and self._owner(legacy) in (None, namespace):
                raise self._migration(legacy)
        owner = self._owner(path)
        if owner is not None and owner != namespace:
            raise self._migration(path)
        # Every underscore in an unmarked old filename could have been a separator in its namespace.
        if path.exists() and owner is None and (encoded or "_" in namespace):
            raise self._migration(path)
        return path

    def _read(self, namespace: str) -> dict[str, Any]:
        path = self._path(namespace)
        if not path.exists():
            return {}
        try:
            return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt memory file '%s', resetting", path)
            return {}

    def _write(self, namespace: str, data: dict[str, Any]) -> None:
        path = self._path(namespace)
        marker = self._checked_path(path.name + ".namespace")
        if not marker.exists():
            self._atomic_write(marker, json.dumps(namespace))
        self._atomic_write(path, json.dumps(data, indent=2, default=str))

    def _atomic_write(self, path: Path, payload: str) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._base_dir, prefix=".pyfly-memory-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, namespace: str, entry: MemoryEntry) -> None:
        with self._lock:
            data = self._read(namespace)
            data[entry.entry_id] = entry.model_dump(mode="json")
            self._write(namespace, data)

    def load(self, namespace: str) -> list[MemoryEntry]:
        with self._lock:
            data = self._read(namespace)
        entries = [MemoryEntry.model_validate(v) for v in data.values()]
        return [e for e in entries if not e.is_expired]

    def load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        for entry in self.load(namespace):
            if entry.key == key:
                return entry
        return None

    def delete(self, namespace: str, entry_id: str) -> None:
        with self._lock:
            data = self._read(namespace)
            data.pop(entry_id, None)
            self._write(namespace, data)

    def clear(self, namespace: str) -> None:
        with self._lock:
            path = self._path(namespace)
            if path.exists():
                path.unlink()

    # -- Async wrappers for non-blocking I/O ---------------------------------

    async def async_save(self, namespace: str, entry: MemoryEntry) -> None:
        """Non-blocking version of :meth:`save`."""
        await asyncio.to_thread(self.save, namespace, entry)

    async def async_load(self, namespace: str) -> list[MemoryEntry]:
        """Non-blocking version of :meth:`load`."""
        return await asyncio.to_thread(self.load, namespace)

    async def async_load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        """Non-blocking version of :meth:`load_by_key`."""
        return await asyncio.to_thread(self.load_by_key, namespace, key)

    async def async_delete(self, namespace: str, entry_id: str) -> None:
        """Non-blocking version of :meth:`delete`."""
        await asyncio.to_thread(self.delete, namespace, entry_id)

    async def async_clear(self, namespace: str) -> None:
        """Non-blocking version of :meth:`clear`."""
        await asyncio.to_thread(self.clear, namespace)


class SQLiteStore:
    """SQLite-backed memory store using stdlib :mod:`sqlite3`.

    Like :class:`FileStore`, this backend is stdlib-only and file-based.
    Compared to :class:`FileStore`, every operation is atomic — a crash
    mid-save cannot corrupt the file — and queries by namespace use a
    SQL index instead of loading and parsing every entry in the
    namespace.

    Choose :class:`FileStore` for the simplest setup and human-readable
    JSON files. Choose :class:`SQLiteStore` for crash safety and faster
    operations on larger namespaces. Existing tables with globally unique entry IDs require an explicit
    migration to ``PRIMARY KEY (namespace, entry_id)``; initialization never rewrites those tables.

    Parameters:
        path: Path to the SQLite file. The file is created if it does
            not exist; parent directories are created as needed.
    """

    _TABLE = "firefly_memory"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # SQLite's transaction context commits or rolls back without closing.
            with closing(sqlite3.connect(str(self._path))) as conn, conn:
                columns = conn.execute(f"PRAGMA table_info({self._TABLE})").fetchall()
                if columns:
                    primary = {column[1] for column in columns if column[5]}
                    global_unique_id = any(
                        index[2]
                        and {column[0] for column in conn.execute("SELECT name FROM pragma_index_info(?)", (index[1],))}
                        == {"entry_id"}
                        for index in conn.execute(f"PRAGMA index_list({self._TABLE})")
                    )
                    if primary != {"namespace", "entry_id"} or global_unique_id:
                        raise DatabaseStoreError(
                            f"SQLite memory table {self._TABLE} requires explicit migration to PRIMARY KEY "
                            "(namespace, entry_id), removing global entry_id uniqueness; no rows were rewritten"
                        )
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._TABLE} (
                        entry_id TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY (namespace, entry_id)
                    )
                    """
                )
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._TABLE}_namespace ON {self._TABLE}(namespace)")
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(f"Failed to initialise SQLite database at {self._path}: {exc}") from exc

    def save(self, namespace: str, entry: MemoryEntry) -> None:
        with closing(sqlite3.connect(str(self._path))) as conn, conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {self._TABLE} (entry_id, namespace, content) VALUES (?, ?, ?)",
                (entry.entry_id, namespace, entry.model_dump_json()),
            )

    def load(self, namespace: str) -> list[MemoryEntry]:
        with closing(sqlite3.connect(str(self._path))) as conn, conn:
            rows = conn.execute(
                f"SELECT content FROM {self._TABLE} WHERE namespace = ? ORDER BY rowid",
                (namespace,),
            ).fetchall()
        entries = [MemoryEntry.model_validate_json(r[0]) for r in rows]
        return [e for e in entries if not e.is_expired]

    def load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        for entry in self.load(namespace):
            if entry.key == key:
                return entry
        return None

    def delete(self, namespace: str, entry_id: str) -> None:
        with closing(sqlite3.connect(str(self._path))) as conn, conn:
            conn.execute(
                f"DELETE FROM {self._TABLE} WHERE namespace = ? AND entry_id = ?",
                (namespace, entry_id),
            )

    def clear(self, namespace: str) -> None:
        with closing(sqlite3.connect(str(self._path))) as conn, conn:
            conn.execute(
                f"DELETE FROM {self._TABLE} WHERE namespace = ?",
                (namespace,),
            )

    # -- Async wrappers for non-blocking I/O ---------------------------------

    async def async_save(self, namespace: str, entry: MemoryEntry) -> None:
        """Non-blocking version of :meth:`save`."""
        await asyncio.to_thread(self.save, namespace, entry)

    async def async_load(self, namespace: str) -> list[MemoryEntry]:
        """Non-blocking version of :meth:`load`."""
        return await asyncio.to_thread(self.load, namespace)

    async def async_load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        """Non-blocking version of :meth:`load_by_key`."""
        return await asyncio.to_thread(self.load_by_key, namespace, key)

    async def async_delete(self, namespace: str, entry_id: str) -> None:
        """Non-blocking version of :meth:`delete`."""
        await asyncio.to_thread(self.delete, namespace, entry_id)

    async def async_clear(self, namespace: str) -> None:
        """Non-blocking version of :meth:`clear`."""
        await asyncio.to_thread(self.clear, namespace)
