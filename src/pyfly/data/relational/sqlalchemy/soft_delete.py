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
- stamps ``updated_at`` and ``updated_by`` where the entity has them (the auditing listener's user);
- leaves a row that is already deleted alone: its ``deleted_at`` keeps the time it was first deleted.

The entities the unit holds, and the entity passed in, are kept in step with the row. Every read excludes
deleted rows; ``find_all_including_deleted``, ``restore`` and ``hard_delete`` reach them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError

from pyfly.data.relational.sqlalchemy.repository import ID, Repository, _state
from pyfly.data.relational.sqlalchemy.statements import unique_entities

T = TypeVar("T")


def _current_auditor() -> str | None:
    """The user the auditing listener stamps ``updated_by`` with (``None`` when nobody is authenticated)."""
    from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener

    return AuditingEntityListener()._get_current_user()


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
            await self._soft_delete_where(session, [None], None)
        else:
            await self._soft_delete_entities(session, list(entities))

    async def delete_all_in_batch(self, entities: Iterable[T] | None = None) -> None:
        """Soft-delete the given entities, or every active row, with bulk ``UPDATE`` statements (no version is
        checked)."""
        session = self._session
        if entities is None:
            await self._soft_delete_where(session, [None], None)
            return
        identities = [identity for entity in entities if (identity := self._identity_of(entity)) is not None]
        if identities:
            await self._soft_delete_identities(session, identities)

    async def delete_all_by_id_in_batch(self, ids: Iterable[ID]) -> None:
        """Soft-delete the rows with these ids with bulk ``UPDATE`` statements."""
        identities = [self._identity(id) for id in ids]
        if identities:
            await self._soft_delete_identities(self._session, identities)

    async def hard_delete(self, id: ID) -> None:
        """Permanently delete an entity, deleted or not (bypass soft delete; cascades run)."""
        session = self._session
        entity = await session.get(self._model, self._key_value(self._identity(id)))
        if entity is not None:
            await session.delete(entity)
            await session.flush()

    async def restore(self, id: ID) -> T | None:
        """Restore a soft-deleted entity by clearing ``deleted_at`` (through the ORM: the version is bumped and
        the audit columns stamped); returns the entity, or ``None`` when the id matches no row."""
        session = self._session
        entity = await session.get(self._model, self._key_value(self._identity(id)))
        if entity is None:
            return None
        if getattr(entity, "deleted_at", None) is not None:
            entity.deleted_at = None  # type: ignore[attr-defined]
            await session.flush()
            await self._load_generated(session, [entity])
        return entity

    # ------------------------------------------------------------------
    # Reads that reach deleted rows
    # ------------------------------------------------------------------

    async def find_all_including_deleted(self, **filters: Any) -> list[T]:
        """Find all entities INCLUDING soft-deleted ones."""
        session = self._session
        resolver = self._filter_resolver()
        stmt = select(self._model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, resolver.resolve(key, usage="filter")) == value)
        return unique_entities(await session.execute(stmt))

    # ------------------------------------------------------------------
    # The soft-delete statement
    # ------------------------------------------------------------------

    async def _soft_delete_entities(self, session: AsyncSession, entities: Sequence[Any]) -> None:
        """Soft-delete each entity by key, checking the version it carries (all at once when unversioned)."""
        await session.flush()  # the unit's pending changes first: they may bump the versions compared below
        mapper = self._mapper
        version_key = (
            mapper.get_property_by_column(mapper.version_id_col).key if mapper.version_id_col is not None else None
        )
        plain: list[tuple[Any, ...]] = []
        for entity in entities:
            identity = self._identity_of(entity)
            if identity is None or (_state(entity).key is None and self._is_new(entity)):
                continue  # a new entity: nothing to delete
            carried = _state(entity).dict.get(version_key) if version_key is not None else None
            if carried is None:
                plain.append(identity)
                continue
            matched = await self._soft_delete_where(session, self._pk_equals(identity), carried, [identity], entity)
            if not matched:
                raise StaleDataError(
                    f"Soft delete of {self._model.__name__} {self._key_value(identity)!r} expected version "
                    f"{carried!r}: the row was changed or deleted since the entity was read"
                )
        if plain:
            unversioned = [entity for entity in entities if self._identity_of(entity) in plain]
            await self._soft_delete_identities(session, plain, unversioned)

    async def _soft_delete_identities(
        self, session: AsyncSession, identities: Sequence[tuple[Any, ...]], extra: Sequence[Any] = ()
    ) -> None:
        for criterion in self._in_ids(session, identities):
            await self._soft_delete_where(session, [criterion], None, identities, *extra)

    async def _soft_delete_where(
        self,
        session: AsyncSession,
        criteria: Sequence[Any],
        version: Any,
        identities: Sequence[tuple[Any, ...]] | None = None,
        *entities: Any,
    ) -> int:
        """Mark the active rows matching *criteria* (``[None]``: every active row) deleted; with *version*, only
        the row still at that version. Returns the rows updated, and brings the unit's copies of those rows (or of
        every active row, without *identities*) and *entities* in step.

        One ``UPDATE`` does it, unless the entity's version comes from a generator SQL cannot reproduce (not a
        plain counter): those rows are loaded and updated through the ORM, which runs the generator."""
        mapper = self._mapper
        model = cast(Any, self._model)
        now = datetime.now(UTC)
        stamps: dict[str, Any] = {"deleted_at": now}
        columns = {attribute.key for attribute in mapper.column_attrs}
        if "updated_at" in columns:
            stamps["updated_at"] = now
        auditor = _current_auditor() if "updated_by" in columns else None
        if auditor is not None:
            stamps["updated_by"] = auditor
        version_key = (
            mapper.get_property_by_column(mapper.version_id_col).key if mapper.version_id_col is not None else None
        )
        conditions = [self._active, *(criterion for criterion in criteria if criterion is not None)]
        if version is not None and version_key is not None:
            conditions.append(getattr(model, version_key) == version)
        if version_key is not None and callable(mapper.version_id_generator) and not _increments(mapper):
            return await self._soft_delete_through_orm(session, conditions, stamps, version_key, entities)
        values: dict[Any, Any] = {getattr(model, key): value for key, value in stamps.items()}
        if version_key is not None and callable(mapper.version_id_generator):
            values[getattr(model, version_key)] = getattr(model, version_key) + 1
        stmt = sa_update(model).where(*conditions).values(values).execution_options(synchronize_session=False)
        result = await session.execute(stmt)
        updated = int(getattr(result, "rowcount", 0) or 0)
        if updated:
            bump = version_key if version_key is not None and callable(mapper.version_id_generator) else None
            self._mark_deleted(session, identities, entities, stamps, bump)
        return updated

    async def _soft_delete_through_orm(
        self,
        session: AsyncSession,
        conditions: Sequence[Any],
        stamps: dict[str, Any],
        version_key: str,
        entities: Sequence[Any],
    ) -> int:
        rows = unique_entities(await session.execute(select(self._model).where(*conditions)))
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
        identities: Sequence[tuple[Any, ...]] | None,
        entities: Sequence[Any],
        stamps: dict[str, Any],
        version_key: str | None,
    ) -> None:
        """Stamp the unit's active copies of the deleted rows (all of them without *identities*) and the active
        *entities*, as the ``UPDATE`` did, without reloading them; an entity already deleted keeps its stamps."""
        wanted = set(identities) if identities is not None else None
        held = [
            entity
            for entity in session.sync_session.identity_map.values()
            if isinstance(entity, self._model) and (wanted is None or tuple(_state(entity).identity or ()) in wanted)
        ]
        generator = self._mapper.version_id_generator
        for entity in {id(entity): entity for entity in (*held, *entities)}.values():
            state = _state(entity)
            if state.dict.get("deleted_at") is not None:
                continue
            for key, value in stamps.items():
                set_committed_value(entity, key, value)
            if version_key is not None and callable(generator) and state.dict.get(version_key) is not None:
                set_committed_value(entity, version_key, generator(state.dict[version_key]))
