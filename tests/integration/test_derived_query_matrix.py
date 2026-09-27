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
"""Derived query methods on every lane: what they match, what they return, and what they run.

- ``and`` binds tighter than ``or`` (C109): ``find_by_a_or_b_and_c`` is ``a OR (b AND c)``, for every prefix.
- ``_containing``, ``_starting_with`` and ``_ending_with`` match their argument as it is: its ``%`` and ``_``
  are plain characters, while ``_like`` takes a pattern (C131).
- Derived queries on soft-delete entities honor the soft delete (C003): a ``SoftDeleteRepository``'s reads
  never see deleted rows and its ``delete_by_*`` soft-deletes the live rows; a plain repository reads through
  the soft-delete criteria and deletes through the ORM, reaching soft-deleted dependents.
- The return annotation shapes the result (``T | None``, ``Page``, ``Slice``, projections), arguments bind by
  position or keyword, and a method that does not match its entity fails when the repository is built
  (C128).
- Statements are built once per shape and reused (C170, C171): ``None`` compares with ``IS NULL``, IN lists are
  one ``= ANY`` bind on PostgreSQL and padded elsewhere, and a list longer than the dialect's limit is split.
- ``exists_by_*`` probes one row (``SELECT 1 ... LIMIT 1``), and every derived method is a repository operation:
  a unit of work of its own outside a transaction, and exceptions translated to the kernel's.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Protocol

import pytest
from sqlalchemy import Boolean, ForeignKey, Integer, String, event, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from pyfly.data import transactional
from pyfly.data.page import Page, Slice
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.projection import projection
from pyfly.data.query_parser import IncorrectResultSizeException
from pyfly.data.relational.sqlalchemy import statements
from pyfly.data.relational.sqlalchemy.entity import Base, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.soft_delete_criteria import including_deleted
from pyfly.kernel.exceptions import DataIntegrityException
from tests.integration._repository_harness import Datasources, dml, repository_datasources, sql_of
from tests.support.backend_matrix import RelationalBackend

# ---------------------------------------------------------------------------------------------------------
# Models and repositories
# ---------------------------------------------------------------------------------------------------------


class DqAccount(Base):
    __tablename__ = "dq_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    owner: Mapped[str] = mapped_column(String(20))
    tag: Mapped[str] = mapped_column(String(20))
    balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(60), default="")
    logged_in: Mapped[bool] = mapped_column(Boolean, default=False)
    terms_and_conditions_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(40), nullable=True)

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.tag}"


@projection
class AccountView(Protocol):
    owner: str
    balance: int | None


class AccountRepository(Repository[DqAccount, int]):
    async def find_by_owner_or_tag_and_balance(self, owner: str, tag: str, balance: int) -> list[DqAccount]: ...

    async def count_by_owner_or_tag_and_balance(self, owner: str, tag: str, balance: int) -> int: ...

    async def exists_by_owner_or_tag_and_balance(self, owner: str, tag: str, balance: int) -> bool: ...

    async def find_by_owner_and_tag_or_owner_and_balance(
        self, first: str, tag: str, second: str, balance: int
    ) -> list[DqAccount]: ...

    async def delete_by_owner_or_tag_and_balance(self, owner: str, tag: str, balance: int) -> int: ...

    async def find_by_name_containing(self, fragment: str) -> list[DqAccount]: ...

    async def find_by_name_starting_with(self, prefix: str) -> list[DqAccount]: ...

    async def find_by_name_ending_with(self, suffix: str) -> list[DqAccount]: ...

    async def find_by_name_like(self, pattern: str) -> list[DqAccount]: ...

    async def find_by_name_containing_ignore_case(self, fragment: str) -> list[DqAccount]: ...

    async def find_by_name_not_containing(self, fragment: str) -> list[DqAccount]: ...

    async def find_by_name(self, name: str) -> DqAccount | None: ...

    async def find_by_balance(self, balance: int) -> DqAccount | None: ...

    async def find_by_logged_in(self, logged_in: bool) -> list[DqAccount]: ...

    async def count_by_terms_and_conditions_accepted(self, accepted: bool) -> int: ...

    async def count_by_logged_in_true(self) -> int: ...

    async def find_by_note(self, note: str | None) -> list[DqAccount]: ...

    async def find_by_note_not(self, note: str | None) -> list[DqAccount]: ...

    async def find_by_note_and_tag(self, note: str | None, tag: str) -> list[DqAccount]: ...

    async def find_by_id_in(self, ids: list[int]) -> list[DqAccount]: ...

    async def count_by_id_in(self, ids: list[int]) -> int: ...

    async def exists_by_id_in(self, ids: list[int]) -> bool: ...

    async def find_by_id_not_in(self, ids: list[int]) -> list[DqAccount]: ...

    async def delete_by_id_in(self, ids: list[int]) -> int: ...

    async def find_by_id_in_order_by_id_desc(self, ids: list[int]) -> list[DqAccount]: ...

    async def exists_by_owner(self, owner: str) -> bool: ...

    async def find_by_tag(self, tag: str, pageable: Pageable) -> Page[DqAccount]: ...

    async def find_by_balance_greater_than_equal(self, balance: int, pageable: Pageable) -> Slice[DqAccount]: ...

    async def find_by_tag_in(self, tags: list[str], sort: Sort) -> list[DqAccount]: ...

    async def find_by_tag_order_by_balance_desc_id_asc(self, tag: str) -> list[DqAccount]: ...

    async def find_by_balance_between(self, low: int, high: int) -> list[AccountView]: ...

    async def find_by_owner(self, owner: str) -> AccountView | None: ...


ROWS = [
    {"id": 1, "owner": "ann", "tag": "x", "balance": 0, "name": "discount 50% off", "logged_in": True,
     "terms_and_conditions_accepted": True, "note": None},
    {"id": 2, "owner": "bob", "tag": "t", "balance": 5, "name": "discount 500 off", "logged_in": False,
     "terms_and_conditions_accepted": False, "note": "n"},
    {"id": 3, "owner": "cat", "tag": "t", "balance": 9, "name": "a_b", "logged_in": True,
     "terms_and_conditions_accepted": False, "note": None},
    {"id": 4, "owner": "dan", "tag": "x", "balance": 5, "name": "axb", "logged_in": False,
     "terms_and_conditions_accepted": True, "note": "n"},
    {"id": 5, "owner": "eve", "tag": "y", "balance": 5, "name": "Plain", "logged_in": False,
     "terms_and_conditions_accepted": False, "note": None},
]  # fmt: skip


class DqOrder(SoftDeleteMixin, Base):
    __tablename__ = "dq_order"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    customer_id: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))


class OrderRepository(SoftDeleteRepository[DqOrder, int]):
    async def find_by_status(self, status: str) -> list[DqOrder]: ...

    async def count_by_status(self, status: str) -> int: ...

    async def exists_by_customer_id(self, customer_id: str) -> bool: ...

    async def delete_by_customer_id(self, customer_id: str) -> int: ...

    async def delete_by_status(self, status: str) -> list[DqOrder]: ...


class OrderArchive(Repository[DqOrder, int]):
    """A plain repository over the soft-delete entity: it reads through the soft-delete criteria."""

    async def find_by_status(self, status: str) -> list[DqOrder]: ...

    async def count_by_status(self, status: str) -> int: ...

    async def delete_by_customer_id(self, customer_id: str) -> int: ...


class DqShelf(Base):
    __tablename__ = "dq_shelf"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(20))
    books: Mapped[list[DqBook]] = relationship(cascade="all, delete-orphan", order_by="DqBook.id")


class DqBook(SoftDeleteMixin, Base):
    __tablename__ = "dq_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("dq_shelf.id"))
    title: Mapped[str] = mapped_column(String(20))


class ShelfRepository(Repository[DqShelf, int]):
    async def delete_by_name(self, name: str) -> int: ...


class DqCustomer(Base):
    __tablename__ = "dq_customer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(20))


class DqInvoice(Base):
    """References a customer with a plain foreign key and no relationship: a bulk delete of the customer fails."""

    __tablename__ = "dq_invoice"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("dq_customer.id"))


class CustomerRepository(Repository[DqCustomer, int]):
    async def delete_by_name(self, name: str) -> int: ...


_TENANT: ContextVar[str] = ContextVar("dq_tenant", default="x")


class TenantAccountRepository(Repository[DqAccount, int]):
    """Its own read criteria, read on every call (a tenant filter from the request's context)."""

    def _criteria(self) -> tuple[Any, ...]:
        return (DqAccount.tag == _TENANT.get(),)

    async def find_by_balance_greater_than_equal(self, balance: int) -> list[DqAccount]: ...

    async def count_by_logged_in(self, logged_in: bool) -> int: ...


MODELS = (DqAccount, DqOrder, DqShelf, DqBook, DqCustomer, DqInvoice)


def _built(repository: Any) -> Any:
    """*repository* as the context builds it: its stubs compiled by the post-processor."""
    return RepositoryBeanPostProcessor().after_init(repository, type(repository).__name__)


async def _accounts(datasources: Datasources) -> AccountRepository:
    async with datasources.engine.begin() as conn:
        await conn.execute(insert(DqAccount), ROWS)
    return _built(AccountRepository())


def _ids(items: list[Any]) -> list[int]:
    return sorted(item.id for item in items)


# ---------------------------------------------------------------------------------------------------------
# WP04-08 (C109): and binds tighter than or
# ---------------------------------------------------------------------------------------------------------


async def test_and_binds_tighter_than_or_for_every_prefix(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        # ann OR (tag t AND balance 5): ann and bob (Spring's PartTree; the left fold gave bob alone).
        assert _ids(await accounts.find_by_owner_or_tag_and_balance("ann", "t", 5)) == [1, 2]
        assert await accounts.count_by_owner_or_tag_and_balance("ann", "t", 5) == 2
        assert await accounts.exists_by_owner_or_tag_and_balance("ann", "zz", 5) is True
        # (ann AND x) OR (dan AND 5)
        assert _ids(await accounts.find_by_owner_and_tag_or_owner_and_balance("ann", "x", "dan", 5)) == [1, 4]
        assert await accounts.delete_by_owner_or_tag_and_balance("ann", "t", 5) == 2
        assert _ids(await accounts.find_all()) == [3, 4, 5]


# ---------------------------------------------------------------------------------------------------------
# WP04-09 (C131): a containing value is matched as it is
# ---------------------------------------------------------------------------------------------------------


async def test_containing_starting_and_ending_match_the_value_as_it_is(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        assert _ids(await accounts.find_by_name_containing("50%")) == [1]
        assert _ids(await accounts.find_by_name_containing("a_b")) == [3]
        assert _ids(await accounts.find_by_name_containing("%")) == [1]
        assert _ids(await accounts.find_by_name_containing("_")) == [3]
        assert _ids(await accounts.find_by_name_containing("/")) == []
        assert _ids(await accounts.find_by_name_starting_with("a_")) == [3]
        assert _ids(await accounts.find_by_name_ending_with("% off")) == [1]
        assert _ids(await accounts.find_by_name_not_containing("%")) == [2, 3, 4, 5]
        assert _ids(await accounts.find_by_name_containing_ignore_case("PLAIN")) == [5]
        # _like takes a pattern: its wildcards stay wildcards.
        assert _ids(await accounts.find_by_name_like("a_b")) == [3, 4]
        assert _ids(await accounts.find_by_name_like("discount 50%")) == [1, 2]


# ---------------------------------------------------------------------------------------------------------
# WP04-07 (C128): results shaped by the annotation, arguments by name, fields read against the entity
# ---------------------------------------------------------------------------------------------------------


async def test_a_single_result_method_returns_the_entity_or_none(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        found = await accounts.find_by_name("a_b")
        assert isinstance(found, DqAccount) and found.id == 3
        assert await accounts.find_by_name("nobody") is None
        with pytest.raises(IncorrectResultSizeException) as raised:
            await accounts.find_by_balance(5)
        assert raised.value.context == {"expected": 1, "actual": 2}


async def test_arguments_bind_by_position_or_keyword(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        assert _ids(await accounts.find_by_owner_or_tag_and_balance(owner="ann", tag="t", balance=5)) == [1, 2]
        assert _ids(await accounts.find_by_owner_or_tag_and_balance("ann", balance=5, tag="t")) == [1, 2]
        with pytest.raises(TypeError, match="find_by_owner_or_tag_and_balance"):
            await accounts.find_by_owner_or_tag_and_balance("ann", "t")


async def test_fields_named_like_keywords_or_connectors(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        assert _ids(await accounts.find_by_logged_in(True)) == [1, 3]
        assert await accounts.count_by_terms_and_conditions_accepted(True) == 2
        assert await accounts.count_by_logged_in_true() == 2


async def test_pages_slices_and_sorts(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        page = await accounts.find_by_tag("x", Pageable.of(1, 1, Sort.by("id")))
        assert (page.total, _ids(page.items)) == (2, [1])
        page = await accounts.find_by_tag(pageable=Pageable.of(2, 1, Sort.by("id")), tag="x")
        assert (page.total, _ids(page.items)) == (2, [4])
        piece = await accounts.find_by_balance_greater_than_equal(5, Pageable.of(1, 2, Sort.by(Order.desc("id"))))
        assert ([item.id for item in piece.items], piece.has_next) == ([5, 4], True)
        ordered = await accounts.find_by_tag_in(["x", "t"], Sort.by(Order.desc("balance"), Order.asc("id")))
        assert [item.id for item in ordered] == [3, 2, 4, 1]
        assert [item.id for item in await accounts.find_by_tag_order_by_balance_desc_id_asc("t")] == [3, 2]


async def test_a_repositorys_own_criteria_are_read_on_every_call(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _accounts(datasources)
        accounts = _built(TenantAccountRepository())
        assert _ids(await accounts.find_by_balance_greater_than_equal(0)) == [1, 4]
        assert await accounts.count_by_logged_in(False) == 1
        token = _TENANT.set("t")
        try:
            assert _ids(await accounts.find_by_balance_greater_than_equal(0)) == [2, 3]
            assert await accounts.count_by_logged_in(False) == 1
        finally:
            _TENANT.reset(token)


async def test_projections(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        views = await accounts.find_by_balance_between(5, 9)
        assert sorted((view.owner, view.balance) for view in views) == [("bob", 5), ("cat", 9), ("dan", 5), ("eve", 5)]
        assert not isinstance(views[0], DqAccount)
        one = await accounts.find_by_owner("cat")
        assert one is not None and (one.owner, one.balance) == ("cat", 9)
        assert await accounts.find_by_owner("nobody") is None


# ---------------------------------------------------------------------------------------------------------
# WP04-02 (C003): soft delete
# ---------------------------------------------------------------------------------------------------------


async def _orders(datasources: Datasources) -> None:
    """Orders 1 and 2 of c1 and 3 of c2, all open; 2 was soft-deleted a long time ago."""
    rows = [
        {"id": 1, "customer_id": "c1", "status": "open", "deleted_at": None},
        {"id": 2, "customer_id": "c1", "status": "open", "deleted_at": datetime(2020, 1, 1, tzinfo=UTC)},
        {"id": 3, "customer_id": "c2", "status": "open", "deleted_at": None},
    ]
    async with datasources.engine.begin() as conn:
        await conn.execute(insert(DqOrder), rows)


async def _stored_orders(datasources: Datasources) -> dict[int, bool]:
    """Every order row, id -> whether it is soft-deleted."""
    async with datasources.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, deleted_at FROM dq_order ORDER BY id"))).all()
    return {row[0]: row[1] is not None for row in rows}


async def test_soft_delete_repository_reads_never_see_deleted_rows(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _orders(datasources)
        orders = _built(OrderRepository())
        assert _ids(await orders.find_by_status("open")) == [1, 3]
        assert await orders.count_by_status("open") == 2
        with including_deleted():  # lifts the criteria only: the repository's own reads stay filtered
            assert _ids(await orders.find_by_status("open")) == [1, 3]
            assert await orders.count_by_status("open") == 2
        assert await orders.exists_by_customer_id("c1") is True
        await orders.delete_by_id(1)
        assert await orders.exists_by_customer_id("c1") is False


async def test_a_plain_repository_reads_through_the_soft_delete_criteria(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _orders(datasources)
        archive = _built(OrderArchive())
        assert _ids(await archive.find_by_status("open")) == [1, 3]
        assert await archive.count_by_status("open") == 2
        with including_deleted():
            assert _ids(await archive.find_by_status("open")) == [1, 2, 3]
            assert await archive.count_by_status("open") == 3


async def test_a_derived_delete_on_a_soft_delete_repository_soft_deletes_the_live_rows(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _orders(datasources)
        orders = _built(OrderRepository())
        assert await orders.delete_by_customer_id("c1") == 1  # order 2 was deleted already
        assert await _stored_orders(datasources) == {1: True, 2: True, 3: False}
        async with datasources.engine.connect() as conn:
            first_deleted = (await conn.execute(text("SELECT deleted_at FROM dq_order WHERE id = 2"))).scalar_one()
        assert str(first_deleted).startswith("2020-01-01")  # an already deleted row keeps its time
        restored = await orders.restore(1)
        assert restored is not None and restored.deleted_at is None
        deleted = await orders.delete_by_status("open")
        assert _ids(deleted) == [1, 3] and all(order.deleted_at is not None for order in deleted)
        assert await _stored_orders(datasources) == {1: True, 2: True, 3: True}


async def test_a_derived_delete_on_a_plain_repository_deletes_what_its_reads_see(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _orders(datasources)
        archive = _built(OrderArchive())
        assert await archive.delete_by_customer_id("c1") == 1
        assert await _stored_orders(datasources) == {2: True, 3: False}
        with including_deleted():
            assert await archive.delete_by_customer_id("c1") == 1
        assert await _stored_orders(datasources) == {3: False}


async def test_a_derived_delete_reaches_soft_deleted_dependents(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        async with datasources.engine.begin() as conn:
            await conn.execute(insert(DqShelf), [{"id": 1, "name": "s1"}, {"id": 2, "name": "s2"}])
            await conn.execute(
                insert(DqBook),
                [
                    {"id": 10, "shelf_id": 1, "title": "live", "deleted_at": None},
                    {"id": 11, "shelf_id": 1, "title": "gone", "deleted_at": datetime(2020, 1, 1, tzinfo=UTC)},
                    {"id": 20, "shelf_id": 2, "title": "kept", "deleted_at": None},
                ],
            )
        shelves = _built(ShelfRepository())
        # The cascade deletes the soft-deleted book too: the foreign key has no ON DELETE action.
        assert await shelves.delete_by_name("s1") == 1
        async with datasources.engine.connect() as conn:
            books = (await conn.execute(text("SELECT id FROM dq_book ORDER BY id"))).scalars().all()
        assert list(books) == [20]


# ---------------------------------------------------------------------------------------------------------
# WP04-11 (C170, C171): statements built once, NULL-aware, portable IN lists
# ---------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _executed_statements() -> Iterator[list[Any]]:
    """The statement objects the ORM is asked to execute (before any listener rewrites them)."""
    seen: list[Any] = []

    def record(state: Any) -> None:
        seen.append(state.statement)

    event.listen(Session, "do_orm_execute", record, insert=True)
    try:
        yield seen
    finally:
        event.remove(Session, "do_orm_execute", record)


async def test_a_derived_statement_is_built_once(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        with _executed_statements() as seen:
            await accounts.find_by_owner_or_tag_and_balance("ann", "t", 5)
            await accounts.find_by_owner_or_tag_and_balance("bob", "x", 0)
            await accounts.count_by_owner_or_tag_and_balance("ann", "t", 5)
            await accounts.count_by_owner_or_tag_and_balance("cat", "y", 1)
        assert len(seen) == 4
        assert seen[0] is seen[1] and seen[2] is seen[3]


async def test_none_compares_with_is_null(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        assert _ids(await accounts.find_by_note(None)) == [1, 3, 5]
        assert _ids(await accounts.find_by_note("n")) == [2, 4]
        assert _ids(await accounts.find_by_note_not(None)) == [2, 4]
        assert _ids(await accounts.find_by_note_not("n")) == []
        assert _ids(await accounts.find_by_note_and_tag(None, "t")) == [3]
        assert _ids(await accounts.find_by_note(None)) == [1, 3, 5]  # the IS NULL variant is kept too


async def test_in_lists(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        assert _ids(await accounts.find_by_id_in([1, 3])) == [1, 3]
        assert _ids(await accounts.find_by_id_in([3, 1, 3, 4, 5])) == [1, 3, 4, 5]
        assert await accounts.find_by_id_in([]) == []
        assert await accounts.count_by_id_in([1, 2, 3]) == 3
        assert await accounts.exists_by_id_in([99]) is False
        assert _ids(await accounts.find_by_id_not_in([1, 2])) == [3, 4, 5]
        assert _ids(await accounts.find_by_id_not_in([])) == [1, 2, 3, 4, 5]
        with datasources.counter() as counter:
            await accounts.find_by_id_in([1, 2, 3])
        (sql,) = sql_of(counter, "SELECT")
        if datasources.dialect == "postgresql":
            assert "= ANY (" in sql  # one array bind, the same statement for every length
        else:
            assert sql.count("?") + sql.count("%s") == 4  # three values padded to four


async def test_an_in_list_longer_than_the_dialect_allows_is_split(
    relational_backend: RelationalBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        dialect = datasources.engine.dialect
        # Two values per statement (beside the binds reserved for the rest of it).
        monkeypatch.setitem(statements._IN_LIMITS, statements.backend_name(dialect), statements.RESERVED_BINDS + 2)
        with datasources.counter() as counter:
            assert _ids(await accounts.find_by_id_in([1, 2, 3, 4, 5, 6])) == [1, 2, 3, 4, 5]
        statements_run = dml(counter).get("SELECT", 0)
        assert statements_run == (1 if datasources.dialect == "postgresql" else 3)
        assert await accounts.count_by_id_in([1, 2, 3, 4, 5, 99]) == 5
        assert await accounts.exists_by_id_in([97, 98, 99, 5]) is True
        assert await accounts.exists_by_id_in([97, 98, 99]) is False
        if datasources.dialect != "postgresql":
            with pytest.raises(ValueError, match="order"):
                await accounts.find_by_id_in_order_by_id_desc([1, 2, 3, 4])
        assert await accounts.delete_by_id_in([1, 2, 3]) == 3
        assert _ids(await accounts.find_all()) == [4, 5]


async def test_exists_probes_one_row(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)
        with datasources.counter() as counter:
            assert await accounts.exists_by_owner("ann") is True
            assert await accounts.exists_by_owner("nobody") is False
        for sql in sql_of(counter, "SELECT"):
            assert "count(" not in sql.lower()
            assert any(clause in sql.upper() for clause in ("LIMIT", "FETCH FIRST", "TOP"))


# ---------------------------------------------------------------------------------------------------------
# Repository operations: units of work and exception translation
# ---------------------------------------------------------------------------------------------------------


async def test_derived_methods_join_the_callers_unit_of_work(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)

        @transactional
        async def delete_then_fail() -> None:
            assert await accounts.delete_by_owner_or_tag_and_balance("ann", "t", 5) == 2
            assert await accounts.count_by_owner_or_tag_and_balance("ann", "t", 5) == 0
            raise RuntimeError("roll back")

        with pytest.raises(RuntimeError):
            await delete_then_fail()
        assert await accounts.count_by_owner_or_tag_and_balance("ann", "t", 5) == 2


async def test_a_bulk_derived_delete_synchronizes_what_the_unit_holds(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        accounts = await _accounts(datasources)

        @transactional
        async def load_then_delete() -> tuple[bool, bool]:
            held = await accounts.find_by_name("a_b")
            assert held is not None
            with datasources.counter() as counter:
                assert await accounts.delete_by_owner_or_tag_and_balance("cat", "zz", 0) == 1
            assert dml(counter).get("DELETE") == 1  # one bulk DELETE: the mapper has nothing to cascade
            return await accounts.exists_by_id(3), await accounts.exists_by_id(4)

        assert await load_then_delete() == (False, True)  # the held entity is gone from the unit too


async def test_a_derived_delete_raises_the_kernels_exception(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        async with datasources.engine.begin() as conn:
            await conn.execute(insert(DqCustomer), [{"id": 1, "name": "acme"}])
            await conn.execute(insert(DqInvoice), [{"id": 1, "customer_id": 1}])
        customers = _built(CustomerRepository())
        with pytest.raises(DataIntegrityException) as raised:
            await customers.delete_by_name("acme")
        assert isinstance(raised.value.__cause__, IntegrityError)
        async with datasources.engine.connect() as conn:
            assert (await conn.execute(select(DqCustomer.id))).scalars().all() == [1]
