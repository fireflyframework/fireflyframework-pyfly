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
"""Repository that performs soft deletes instead of hard deletes.

A soft delete is one ``UPDATE ... SET deleted_at = :now WHERE <primary key> AND deleted_at IS NULL``, by
primary key, so it works for any entity: one the unit holds, a detached one (every entity a repository call
returns outside a transaction is), or one of another session. It also:

- bumps the version of a versioned entity, so a stale copy can no longer be saved over the deleted row, and
  checks the version an entity carries (``StaleDataError`` when it is stale);
- stamps ``updated_at`` and ``updated_by`` where the entity has them, as the auditing listener stamps an ORM
  update: the active auditing handler's time and auditor (:mod:`pyfly.data.auditing`: the application's
  ``DateTimeProvider`` and ``AuditorAware``, the defaults when none is registered), ``updated_by`` ``None``
  when there is no auditor;
- leaves a row that is already deleted alone: its ``deleted_at`` keeps the time it was first deleted.

The entities the unit holds, and the entity passed in, are kept in step with the row: where the dialect has
``UPDATE ... RETURNING`` (PostgreSQL, SQLite), exactly the copies of the rows the ``UPDATE`` changed; on MySQL
and MariaDB, the copies of the requested keys that are active in memory, which is wrong only for a copy whose
row another transaction deleted since it was read. Long id lists go one ``UPDATE`` per chunk, and each chunk
leaves room for the values the ``UPDATE`` sets (SQLite and SQL Server count them against the same limit).
Every read excludes deleted rows; ``find_all_including_deleted``, ``restore`` and ``hard_delete`` reach them
(``hard_delete`` also deletes the soft-deleted dependents its cascades reach:
``soft_delete_criteria.hard_delete``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, TypeVar, cast

from sqlalchemy import Update, and_, select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper, load_only, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError

from pyfly.data.auditing import AuditingHandler, active_auditing_handler
from pyfly.data.relational.sqlalchemy.repository import ID, Repository, _state
from pyfly.data.relational.sqlalchemy.soft_delete_criteria import hard_delete as delete_for_good
from pyfly.data.relational.sqlalchemy.soft_delete_criteria import (
    including_deleted,
    reaches_soft_deleted_rows,
)
from pyfly.data.relational.sqlalchemy.statements import RESERVED_BINDS, dialect_of, unique_entities

T = TypeVar("T")


def _increments(mapper: Mapper[Any]) -> bool:
    """Whether the mapper's version generator is a plain counter, so ``version + 1`` does in SQL what the ORM
    does in Python."""
    generator = mapper.version_id_generator
    if not callable(generator):
        return False
    try:
        return bool(generator(None) == 1 and generator(1) == 2 and generator(41) == 42)
    except Exception:  # noqa: BLE001 — a generator that cannot count is not a counter
        return False


class SoftDeleteRepository(Repository[T, ID]):
    """Repository that performs soft deletes instead of hard deletes.

    Entities must use :class:`SoftDeleteMixin` to have a ``deleted_at`` column.
    All find methods automatically exclude soft-deleted entities.

    Like :class:`Repository`, it resolves its session per call: every method joins the current unit of
    work, or runs in an auto unit of its own (a read unit for ``find*``/``count*``/``exists*``/``stream*``,
    a write unit that commits for the soft-delete writes and ``restore``). See the module documentation for
    what a soft delete does.
    """

    # Its methods, like Repository's, are framework operations: atomic for a task that shares the unit.
    _pyfly_framework_repository = True

    @property
    def _active(self) -> Any:
        return self._model.deleted_at == None  # type: ignore[attr-defined]  # noqa: E711

    def _criteria(self) -> tuple[Any, ...]:
        return (self._active,)

    def _visible(self, entity: Any) -> bool:
        return getattr(entity, "deleted_at", None) is None

    def _held_visible(self, entity: Any) -> bool | None:
        state = _state(entity)
        if "deleted_at" not in state.dict:
            return None  # not loaded: ask the database
        return state.dict["deleted_at"] is None

    def _active_select(self, **filters: Any) -> Any:
        """A ``SELECT`` of the active (not deleted) entities with equality *filters*, for a subclass's own
        queries: the same statement every read method starts from (``_filtered_select``)."""
        return self._filtered_select(**filters)

    # ------------------------------------------------------------------
    # Soft-delete writes
    # ------------------------------------------------------------------

    async def delete(self, entity: T) -> None:
        """Soft-delete an entity by its primary key (the version it carries is checked)."""
        await self._soft_delete_entities(self._session, [entity])

    async def delete_by_id(self, id: ID) -> None:
        """Soft-delete by id: set ``deleted_at`` instead of removing from DB (a missing id is ignored)."""
        await self._soft_delete_identities(self._session, [self._identity(id)])

    async def delete_all_by_id(self, ids: Iterable[ID]) -> None:
        """Soft-delete all entities with given IDs (one ``UPDATE`` per id chunk)."""
        identities = [self._identity(id) for id in ids]
        if identities:
            await self._soft_delete_identities(self._session, identities)

    async def delete_all(self, entities: Iterable[T] | None = None) -> None:
        """Soft-delete the given entities (each version checked), or ALL active rows when ``entities`` is
        ``None``."""
        session = self._session
        if entities is None:
            await self._soft_delete(session, [None], await self._stamps(), None, None, ())
        else:
            await self._soft_delete_entities(session, list(entities))

    async def delete_all_in_batch(self, entities: Iterable[T] | None = None) -> None:
        """Soft-delete the given entities, or every active row, with bulk ``UPDATE`` statements (no version is
        checked; an entity only pending in the unit is simply not inserted)."""
        session = self._session
        if entities is None:
            await self._soft_delete(session, [None], await self._stamps(), None, None, ())
            return
        stored = self._expunge_pending(session, list(entities))
        identities = [identity for entity in stored if (identity := self._identity_of(entity)) is not None]
        if identities:
            await self._soft_delete_identities(session, identities, stored)

    async def delete_all_by_id_in_batch(self, ids: Iterable[ID]) -> None:
        """Soft-delete the rows with these ids with bulk ``UPDATE`` statements."""
        identities = [self._identity(id) for id in ids]
        if identities:
            await self._soft_delete_identities(self._session, identities)

    async def hard_delete(self, id: ID) -> None:
        """Permanently delete an entity, deleted or not (bypass soft delete; cascades run, and reach soft-deleted
        dependents)."""
        session = self._session
        with including_deleted():
            entity = await session.get(self._model, self._key_value(self._identity(id)))
        if entity is not None:
            await delete_for_good(session, entity)

    async def restore(self, id: ID) -> T | None:
        """Restore a soft-deleted entity by clearing ``deleted_at`` (through the ORM: the version is bumped and
        the audit columns stamped); returns the entity, or ``None`` when the id matches no row.

        The entity is read with ``include_deleted``, which its eager loads inherit: it is returned as a live
        entity, so the relationships to soft-delete entities it has loaded are loaded again, without the
        deleted rows (one ``SELECT`` of its key and one per relationship), also when it was live already."""
        session = self._session
        identity = self._identity(id)
        entity = await session.get(self._model, self._key_value(identity), execution_options={"include_deleted": True})
        if entity is None:
            return None
        state = _state(entity)
        if "deleted_at" not in state.dict:
            await session.refresh(entity, ["deleted_at"])  # expired in the unit: never load it on access
        restoring = state.dict.get("deleted_at") is not None
        if restoring:
            entity.deleted_at = None  # type: ignore[attr-defined]
            await session.flush()
        revealed = [
            relationship.key
            for relationship in self._mapper.relationships
            if relationship.key in state.dict
            and relationship.lazy not in (None, "noload")
            and reaches_soft_deleted_rows(relationship)
        ]
        if revealed:
            session.expire(entity, revealed)
            options = [selectinload(getattr(self._model, key)) for key in revealed]
            only = load_only(*self._pk_attributes)
            await session.execute(select(self._model).where(*self._pk_equals(identity)).options(only, *options))
        if restoring:
            await self._load_generated(session, [entity])
        return entity

    # ------------------------------------------------------------------
    # Reads that reach deleted rows
    # ------------------------------------------------------------------

    async def find_all_including_deleted(self, **filters: Any) -> list[T]:
        """Find all entities INCLUDING soft-deleted ones."""
        session = self._session
        stmt = select(self._model).where(*self._filter_criteria(filters)).execution_options(include_deleted=True)
        return unique_entities(await session.execute(stmt))

    # ------------------------------------------------------------------
    # The soft-delete statement
    # ------------------------------------------------------------------

    @property
    def _version_key(self) -> str | None:
        mapper = self._mapper
        return mapper.get_property_by_column(mapper.version_id_col).key if mapper.version_id_col is not None else None

    def _counts_versions(self) -> bool:
        """Whether the ``UPDATE`` bumps the version itself (``version + 1``)."""
        return self._version_key is not None and callable(self._mapper.version_id_generator)

    async def _stamps(self) -> dict[str, Any]:
        """The columns a soft delete sets: ``deleted_at``, and ``updated_at``/``updated_by`` where the entity has
        them, from the active auditing handler (the defaults when none is registered), as the auditing listener
        stamps an ORM update: ``updated_by`` is the current auditor, ``None`` when there is none."""
        handler = active_auditing_handler() or AuditingHandler()
        now = handler.now()
        stamps: dict[str, Any] = {"deleted_at": now}
        columns = {attribute.key for attribute in self._mapper.column_attrs}
        if "updated_at" in columns:
            stamps["updated_at"] = now
        if "updated_by" in columns:
            stamps["updated_by"] = await handler.current_auditor()
        return stamps

    def _soft_delete_update(self, criteria: Sequence[Any], stamps: dict[str, Any], version: Any = None) -> Update:
        """``UPDATE ... SET <stamps>[, version = version + 1] WHERE deleted_at IS NULL AND <criteria>`` (``None``
        criteria match every row), and ``AND version = :version`` with *version*."""
        model = cast(Any, self._model)
        version_key = self._version_key
        conditions = [self._active, *(criterion for criterion in criteria if criterion is not None)]
        if version is not None and version_key is not None:
            conditions.append(getattr(model, version_key) == version)
        values: dict[Any, Any] = {getattr(model, key): value for key, value in stamps.items()}
        if version_key is not None and self._counts_versions():
            values[getattr(model, version_key)] = getattr(model, version_key) + 1
        return sa_update(model).where(*conditions).values(values).execution_options(synchronize_session=False)

    def _soft_delete_criteria(
        self, dialect: Dialect, identities: Sequence[tuple[Any, ...]], stamps: dict[str, Any]
    ) -> list[Any]:
        """The id criteria of a soft delete of *identities*, one ``UPDATE`` each: every chunk leaves room for the
        values the ``UPDATE`` sets beside it (SQLite and SQL Server count them against the same limit)."""
        sets = len(stamps) + (1 if self._counts_versions() else 0)
        return self._id_criteria(dialect, identities, reserved=RESERVED_BINDS + sets)

    async def _soft_delete_entities(self, session: AsyncSession, entities: Sequence[Any]) -> None:
        """Soft-delete each entity by key, checking the version it carries (all at once when unversioned)."""
        stored = self._expunge_pending(session, entities)
        await session.flush()  # the unit's pending changes first: they may bump the versions compared below
        version_key = self._version_key
        stamps = await self._stamps()
        plain: dict[tuple[Any, ...], list[Any]] = {}
        for entity in stored:
            state = _state(entity)
            identity = self._identity_of(entity)
            if identity is None or (state.key is None and self._is_new(entity)):
                continue  # a new entity: nothing to delete
            carried = state.dict.get(version_key) if version_key is not None else None
            if carried is None:
                plain.setdefault(identity, []).append(entity)
                continue
            criterion = and_(*self._pk_equals(identity))
            if not await self._soft_delete(session, [criterion], stamps, carried, [identity], [entity]):
                raise StaleDataError(
                    f"Soft delete of {self._model.__name__} {self._key_value(identity)!r} expected version "
                    f"{carried!r}: the row was changed or deleted since the entity was read"
                )
        if plain:
            unversioned = [entity for entities_of in plain.values() for entity in entities_of]
            await self._soft_delete_identities(session, list(plain), unversioned, stamps)

    async def _soft_delete_identities(
        self,
        session: AsyncSession,
        identities: Sequence[tuple[Any, ...]],
        entities: Sequence[Any] = (),
        stamps: dict[str, Any] | None = None,
    ) -> None:
        stamps = await self._stamps() if stamps is None else stamps
        criteria = self._soft_delete_criteria(dialect_of(session), identities, stamps)
        await self._soft_delete(session, criteria, stamps, None, identities, entities)

    async def _soft_delete(
        self,
        session: AsyncSession,
        criteria: Sequence[Any],
        stamps: dict[str, Any],
        version: Any,
        identities: Sequence[tuple[Any, ...]] | None,
        entities: Sequence[Any],
    ) -> int:
        """Mark the active rows matching any of *criteria* deleted (one ``UPDATE`` per criterion; ``None``: every
        active row), with *version* only the row still at that version, and return how many rows it updated.

        The unit's copies of the updated rows and *entities* are brought in step without reloading them. Where
        the dialect has ``UPDATE ... RETURNING`` and there is a copy to bring in step, the ``UPDATE`` returns the
        keys it changed and exactly those copies are stamped; elsewhere (MySQL, MariaDB) the copies of the
        requested keys (of every row, without *identities*) that are active in memory are, which is wrong only
        for a copy whose row another transaction deleted since it was read. Every active row (no *identities*)
        is updated in two steps there, so what it returns does not grow with the table: the rows of the keys
        the unit holds first, returning their keys, then all the others, returning nothing. A version SQL cannot
        compute (not a plain counter) is left to the ORM, which runs the generator: those rows are loaded and
        updated there.
        """
        version_key = self._version_key
        if version_key is not None and self._counts_versions() and not _increments(self._mapper):
            return await self._soft_delete_through_orm(session, criteria, stamps, version, version_key, entities)
        returning = dialect_of(session).update_returning and bool(entities or self._holds_entities(session))
        returns = [returning] * len(criteria)  # which UPDATE returns the keys it changed
        if returning and identities is None:
            held = self._held_identities(session, entities)
            first = self._soft_delete_criteria(dialect_of(session), held, stamps)
            criteria = [*first, *criteria]
            returns = [True] * len(first) + [False] * (len(criteria) - len(first))
        changed: set[tuple[Any, ...]] = set()
        updated = 0
        for criterion, returned in zip(criteria, returns, strict=True):
            statement = self._soft_delete_update([criterion], stamps, version)
            if returned:
                rows = (await session.execute(statement.returning(*self._pk_attributes))).all()
                changed.update(tuple(row) for row in rows)
                updated += len(rows)
            else:
                result = await session.execute(statement)
                updated += int(getattr(result, "rowcount", 0) or 0)
        if updated:
            if not returning:
                changed_or_all = set(identities) if identities is not None else None
                self._mark_deleted(session, changed_or_all, entities, stamps)
            else:
                self._mark_deleted(session, changed, entities, stamps)
        return updated

    def _held_identities(self, session: AsyncSession, entities: Sequence[Any]) -> list[tuple[Any, ...]]:
        """The keys of the unit's persistent copies of the model and of *entities*, each once."""
        held = (entity for entity in session.sync_session.identity_map.values() if isinstance(entity, self._model))
        keys = (self._identity_of(entity) for entity in (*held, *entities))
        return list(dict.fromkeys(key for key in keys if key is not None))

    async def _soft_delete_through_orm(
        self,
        session: AsyncSession,
        criteria: Sequence[Any],
        stamps: dict[str, Any],
        version: Any,
        version_key: str,
        entities: Sequence[Any],
    ) -> int:
        model = cast(Any, self._model)
        rows: list[Any] = []
        for criterion in criteria:
            conditions = [self._active, *([criterion] if criterion is not None else [])]
            if version is not None:
                conditions.append(getattr(model, version_key) == version)
            rows.extend(unique_entities(await session.execute(select(self._model).where(*conditions))))
        for row in rows:
            for key, value in stamps.items():
                setattr(row, key, value)
        await session.flush()
        by_identity = {tuple(_state(row).identity or ()): row for row in rows}
        for entity in entities:
            row = by_identity.get(self._identity_of(entity) or ())
            if row is not None and row is not entity:
                for key in (*stamps, version_key):
                    set_committed_value(entity, key, getattr(row, key))
        return len(rows)

    def _mark_deleted(
        self,
        session: AsyncSession,
        changed: set[tuple[Any, ...]] | None,
        entities: Sequence[Any],
        stamps: dict[str, Any],
    ) -> None:
        """Stamp the unit's copies of the *changed* rows (of every row with ``None``) and those of *entities*, as
        the ``UPDATE`` did, without reloading them; a copy already deleted in memory keeps its stamps."""
        version_key = self._version_key if self._counts_versions() else None
        held = [
            entity
            for entity in session.sync_session.identity_map.values()
            if isinstance(entity, self._model) and (changed is None or tuple(_state(entity).identity or ()) in changed)
        ]
        touched = [entity for entity in entities if changed is None or self._identity_of(entity) in changed]
        generator = self._mapper.version_id_generator
        for entity in {id(entity): entity for entity in (*held, *touched)}.values():
            state = _state(entity)
            if state.dict.get("deleted_at") is not None:
                continue
            for key, value in stamps.items():
                set_committed_value(entity, key, value)
            if version_key is not None and callable(generator) and state.dict.get(version_key) is not None:
                set_committed_value(entity, version_key, generator(state.dict[version_key]))
