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
"""``SqlAlchemyFlagStore`` (``sources.store.driver=database``): the store on the tables shared with LaraFly.

Each write runs in one transaction boundary: it reads the row, checks
``expected_version``, then updates the row with ``WHERE version = <read>`` (or inserts it) and appends the change,
so a concurrent writer either loses the conditional update or the primary-key insert. Without an
``expected_version`` an owned write retries such a race up to three times; with one it is a conflict. A caller-owned
write uses a savepoint and reports a race as a conflict: its transaction may retain an old snapshot. Reads never use
a read-only unit: a replica could serve a revision older than the rows, or the reverse.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, delete, event, func, insert, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, SessionTransaction

from pyfly.data.relational.framework_schema import (
    FEATURE_FLAG_CHANGES,
    FEATURE_FLAGS,
    ensure_tables,
    feature_flag_changes_table,
    feature_flags_table,
)
from pyfly.data.transaction import (
    Propagation,
    TransactionTemplate,
    after_commit,
    current_unit_of_work,
    infrastructure_unit,
    is_transaction_active,
    register_synchronization,
    resolve_manager,
)
from pyfly.data.transaction.synchronization import CompletionStatus, TransactionSynchronizationAdapter
from pyfly.feature_flags.definitions import parse_document
from pyfly.feature_flags.store.ports import FlagChange, FlagConflictError, FlagNotStoredError, StoredFlag

__all__ = ["SqlAlchemyFlagStore"]

_ATTEMPTS = 3


class _Raced(Exception):
    """Another writer changed the row between this write's read and its write."""


def _record_changed(error: OperationalError) -> bool:
    """MariaDB reports a racing row as ER_CHECKREAD (1020), rather than a zero-row update."""
    args: tuple[Any, ...] = getattr(error.orig, "args", ())
    return bool(args) and args[0] == 1020


def _dump(definition: Mapping[str, Any]) -> str:
    return json.dumps(definition, ensure_ascii=False, separators=(",", ":"))


def _load(text: str | None) -> dict[str, Any] | None:
    return json.loads(text) if text is not None else None


class _SavepointCommitCallback(TransactionSynchronizationAdapter):
    """A callback belongs to every savepoint surrounding its write, even after their release."""

    def __init__(self, session: Session, callback: Callable[[], Awaitable[None]]) -> None:
        self._session = session
        self._callback = callback
        self._rolled_back = False
        self._ancestors: set[SessionTransaction] = set()
        transaction = session.get_nested_transaction()
        while transaction is not None:
            self._ancestors.add(transaction)
            transaction = transaction.parent
        event.listen(session, "after_soft_rollback", self._rollback)

    def _rollback(self, session: Session, transaction: SessionTransaction) -> None:
        if transaction in self._ancestors:
            self._rolled_back = True

    async def after_commit(self) -> None:
        if not self._rolled_back:
            await self._callback()

    async def after_completion(self, status: CompletionStatus) -> None:
        event.remove(self._session, "after_soft_rollback", self._rollback)


class SqlAlchemyFlagStore:
    """A ``FlagStore`` on any SQLAlchemy backend. *target* is an ``AsyncEngine``, a registry ``DataSource`` or a
    datasource name; with *create_tables* false the tables are only checked at :meth:`start`."""

    def __init__(
        self,
        target: Any,
        *,
        flags_table: str = FEATURE_FLAGS,
        changes_table: str = FEATURE_FLAG_CHANGES,
        create_tables: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = target
        self._flags = feature_flags_table(flags_table)
        self._changes = feature_flag_changes_table(changes_table)
        self._create_tables = create_tables
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def tables(self) -> tuple[Table, Table]:
        return self._flags, self._changes

    async def start(self) -> None:
        await ensure_tables(self._target, self._flags, self._changes, create=self._create_tables)

    async def stop(self) -> None:
        return None  # the engine is the datasource's, not the store's

    def _row(self, row: Any) -> StoredFlag:
        return StoredFlag(row.flag_key, json.loads(row.definition), int(row.version), row.updated_at, row.updated_by)

    async def all(self) -> dict[str, StoredFlag]:
        flags = self._flags
        statement = select(
            flags.c.flag_key, flags.c.definition, flags.c.version, flags.c.updated_at, flags.c.updated_by
        )
        async with infrastructure_unit(self._target) as session:
            rows = (await session.execute(statement)).all()
        return {row.flag_key: self._row(row) for row in rows}

    async def get(self, key: str) -> StoredFlag | None:
        flags = self._flags
        statement = select(
            flags.c.flag_key, flags.c.definition, flags.c.version, flags.c.updated_at, flags.c.updated_by
        ).where(flags.c.flag_key == key)
        async with infrastructure_unit(self._target) as session:
            row = (await session.execute(statement)).first()
        return self._row(row) if row is not None else None

    async def revision(self) -> int:
        statement = select(func.coalesce(func.max(self._changes.c.id), 0))
        async with infrastructure_unit(self._target) as session:
            return int((await session.execute(statement)).scalar_one())

    @asynccontextmanager
    async def _write_session(self) -> AsyncIterator[Any]:
        async with infrastructure_unit(self._target) as session:
            connection = await session.connection()
            if connection.dialect.name == "sqlite":
                raw = await connection.get_raw_connection()
                if not raw.driver_connection.in_transaction:
                    # Bare aiosqlite engines defer BEGIN; a released savepoint would otherwise commit by itself.
                    await connection.exec_driver_sql("BEGIN IMMEDIATE")
            async with TransactionTemplate(self._target, propagation=Propagation.NESTED).transaction():
                yield session

    async def put(
        self, key: str, definition: Mapping[str, Any], *, actor: str | None, expected_version: int | None = None
    ) -> FlagChange:
        payload = _dump(parse_document({"flags": {key: definition}}).flags[key])
        for _ in range(_ATTEMPTS):
            try:
                return await self._put_once(key, payload, actor, expected_version)
            except (_Raced, OperationalError) as raced:
                if isinstance(raced, OperationalError) and not _record_changed(raced):
                    raise
                if expected_version is not None or is_transaction_active(resolve_manager(self._target).datasource):
                    raise FlagConflictError(key, expected_version, None) from raced
                continue
        raise FlagConflictError(key, expected_version, None)

    async def _put_once(self, key: str, payload: str, actor: str | None, expected: int | None) -> FlagChange:
        flags, changes = self._flags, self._changes
        now = self._clock()
        async with self._write_session() as session:
            current = (
                await session.execute(select(flags.c.definition, flags.c.version).where(flags.c.flag_key == key))
            ).first()
            actual = int(current.version) if current is not None else 0
            if expected is not None and expected != actual:
                raise FlagConflictError(key, expected, actual)
            if current is None:
                values = {"flag_key": key, "definition": payload, "version": 1, "updated_at": now, "updated_by": actor}
                try:
                    await session.execute(insert(flags).values(**values))
                except IntegrityError as raced:
                    if expected is not None:
                        raise FlagConflictError(key, expected, None) from raced
                    raise _Raced from raced
            else:
                result = await session.execute(
                    update(flags)
                    .where(flags.c.flag_key == key, flags.c.version == actual)
                    .values(definition=payload, version=actual + 1, updated_at=now, updated_by=actor)
                )
                if int(result.rowcount) != 1:
                    if expected is not None:
                        raise FlagConflictError(key, expected, None)
                    raise _Raced
            previous = current.definition if current is not None else None
            inserted = await session.execute(
                insert(changes).values(
                    flag_key=key, action="put", definition=payload, previous=previous, actor=actor, changed_at=now
                )
            )
            change_id = int(inserted.inserted_primary_key[0])
        return FlagChange(change_id, key, "put", _load(payload), _load(previous), actor, now)

    async def delete(self, key: str, *, actor: str | None, expected_version: int | None = None) -> FlagChange:
        for _ in range(_ATTEMPTS):
            try:
                return await self._delete_once(key, actor, expected_version)
            except (_Raced, OperationalError) as raced:
                if isinstance(raced, OperationalError) and not _record_changed(raced):
                    raise
                if expected_version is not None or is_transaction_active(resolve_manager(self._target).datasource):
                    raise FlagConflictError(key, expected_version, None) from raced
                continue
        raise FlagConflictError(key, expected_version, None)

    async def _delete_once(self, key: str, actor: str | None, expected: int | None) -> FlagChange:
        flags, changes = self._flags, self._changes
        now = self._clock()
        async with self._write_session() as session:
            current = (
                await session.execute(select(flags.c.definition, flags.c.version).where(flags.c.flag_key == key))
            ).first()
            if current is None:
                raise FlagNotStoredError(key)
            actual = int(current.version)
            if expected is not None and expected != actual:
                raise FlagConflictError(key, expected, actual)
            result = await session.execute(delete(flags).where(flags.c.flag_key == key, flags.c.version == actual))
            if int(result.rowcount) != 1:
                if expected is not None:
                    raise FlagConflictError(key, expected, None)
                raise _Raced
            inserted = await session.execute(
                insert(changes).values(
                    flag_key=key,
                    action="delete",
                    definition=None,
                    previous=current.definition,
                    actor=actor,
                    changed_at=now,
                )
            )
            change_id = int(inserted.inserted_primary_key[0])
        return FlagChange(change_id, key, "delete", None, _load(current.definition), actor, now)

    @property
    def transaction_active(self) -> bool:
        return is_transaction_active(resolve_manager(self._target).datasource)

    async def after_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run after this store's transaction commits, pruning writes undone by a caller savepoint."""
        datasource = resolve_manager(self._target).datasource
        unit = current_unit_of_work(datasource)
        if unit is not None and unit.resource.sync_session.get_nested_transaction() is not None:
            synchronization = _SavepointCommitCallback(unit.resource.sync_session, callback)
            register_synchronization(synchronization, datasource=datasource)
        else:
            await after_commit(callback, datasource=datasource)

    async def history(self, key: str, limit: int = 50) -> list[FlagChange]:
        changes = self._changes
        statement = (
            select(
                changes.c.id,
                changes.c.flag_key,
                changes.c.action,
                changes.c.definition,
                changes.c.previous,
                changes.c.actor,
                changes.c.changed_at,
            )
            .where(changes.c.flag_key == key)
            .order_by(changes.c.id.desc())
            .limit(limit)
        )
        async with infrastructure_unit(self._target) as session:
            rows = (await session.execute(statement)).all()
        return [
            FlagChange(
                int(row.id),
                row.flag_key,
                row.action,
                _load(row.definition),
                _load(row.previous),
                row.actor,
                row.changed_at,
            )
            for row in rows
        ]
