# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``WalletRepository`` — a Spring-Data-style repository on the framework.

Instead of hand-rolling a hexagonal port plus an adapter, Lumen subclasses
the framework's generic
:class:`~pyfly.data.relational.sqlalchemy.Repository`. Declaring
``Repository[WalletEntity, str]`` tells the framework the **entity type**
(``WalletEntity``) and the **primary-key type** (``str``); from that it
provides the full Spring-parity async repository surface out of the box
(``CrudRepository`` → ``ReactiveSortingRepository`` →
``PagingAndSortingRepository``) — ``save``/``save_all``, ``find_by_id``,
``find_all`` (no-arg, ``find_all(Sort)``, ``find_all(Pageable) -> Page``),
``stream_all``, ``exists_by_id``, ``count``, ``delete``/``delete_by_id``,
``delete_all_by_id``/``delete_all``, plus
``find_all_by_spec``/``find_all_by_spec_paged`` — with the ``AsyncSession``
injected by the relational auto-configuration.

On top of the inherited methods this repository adds two things the way
Spring Data does:

* a **derived query** — ``find_by_owner_id`` is declared as a stub
  (``...``); at startup the ``RepositoryBeanPostProcessor`` parses the
  method *name* and compiles a real ``SELECT … WHERE owner_id = :owner_id``;
* a **Specification query** — ``find_rich`` composes a reusable
  :class:`~pyfly.data.relational.sqlalchemy.Specification` predicate and
  runs it with pagination + sorting via ``find_all_by_spec_paged``.

``upsert`` is a thin convenience over SQLAlchemy's ``session.merge`` so a
command handler can persist an entity whether it is new (INSERT) or
already exists (UPDATE) with a single call — the aggregate owns its id, so
both cases key on the same primary key.
"""

from __future__ import annotations

from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data import Page, Pageable
from pyfly.data.relational.sqlalchemy import Repository, Specification


def balance_at_least(min_minor: int) -> Specification[WalletEntity]:
    """A reusable predicate: wallets whose balance is at least *min_minor*.

    Returned as a :class:`Specification`, so it composes with other
    predicates via ``&`` / ``|`` / ``~`` and runs through the repository's
    ``find_all_by_spec`` / ``find_all_by_spec_paged`` methods.
    """
    return Specification(lambda root, q: q.where(root.balance_minor >= min_minor))


@repository
class WalletRepository(Repository[WalletEntity, str]):
    """CRUD + derived + specification queries for :class:`WalletEntity`.

    The ``@repository`` stereotype registers this as a DI bean. The
    framework reads the entity/PK types from the
    ``Repository[WalletEntity, str]`` base and injects the shared
    ``AsyncSession``; inside a unit of work that session is swapped for the
    transactional one by ``@transactional`` (see the command handlers).
    """

    # --- derived query: compiled from the method name by the post-processor
    async def find_by_owner_id(self, owner_id: str) -> list[WalletEntity]:
        """All wallets owned by *owner_id* (derived query stub)."""
        ...

    # --- specification query: composable predicate + pagination ----------
    async def find_rich(self, min_minor: int, pageable: Pageable) -> Page[WalletEntity]:
        """A page of wallets whose balance is at least *min_minor*.

        Builds the predicate with :func:`balance_at_least` and runs it
        through the inherited ``find_all_by_spec_paged``, which applies the
        ``WHERE``, the ``Pageable``'s sort, and ``LIMIT/OFFSET`` and returns
        a :class:`~pyfly.data.Page` with total-count metadata.
        """
        return await self.find_all_by_spec_paged(balance_at_least(min_minor), pageable)

    # --- upsert: one call for both INSERT and UPDATE ---------------------
    async def upsert(self, entity: WalletEntity) -> WalletEntity:
        """Insert *entity* or update the existing row with the same id.

        Uses ``session.merge`` so a freshly-mapped entity carrying the
        aggregate's id persists correctly whether or not a row already
        exists — the aggregate owns its primary key, so there is never an
        ambiguous identity. Flushes so the write is visible within the
        current unit of work; the surrounding ``@transactional`` commits it.
        """
        session = self._require_session()
        merged = await session.merge(entity)
        await session.flush()
        return merged
