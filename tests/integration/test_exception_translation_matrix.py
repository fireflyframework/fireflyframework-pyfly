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
"""Persistence exceptions are the kernel's, the same on every backend (C158, C159).

Repositories and units of work used to leak SQLAlchemy's exception types: service code had to import
``sqlalchemy`` to handle a duplicate or an optimistic-locking conflict, and the conflict ``@Version`` exists
for reached clients as a 500. Repository calls and unit-of-work commits now raise
:class:`~pyfly.kernel.exceptions.DataIntegrityException` (:class:`~pyfly.kernel.exceptions.DuplicateKeyException`
for a unique key) and :class:`~pyfly.kernel.exceptions.OptimisticLockingFailureException`, chained from the
driver's error, with the violated constraint's name (the same on every backend, thanks to the naming
convention) and no SQL or bound values in the message. The sqlite-file lane runs in the fast suite, the
server lanes in the integration suite.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from pyfly.container import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, VersionedMixin
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.session import SessionProvider
from pyfly.data.transaction import Propagation, TransactionTemplate
from pyfly.kernel.exceptions import (
    ConcurrencyException,
    ConflictException,
    DataIntegrityException,
    DuplicateKeyException,
    OptimisticLockingFailureException,
)
from tests.support.backend_matrix import RelationalBackend


class TranslationOwner(Base):
    __tablename__ = "xl_owner"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)


class TranslationAccount(VersionedMixin, BaseEntity):
    __tablename__ = "xl_account"
    __table_args__ = (CheckConstraint("balance >= 0", name="ck_xl_account_balance_positive"),)

    email: Mapped[str] = mapped_column(String(100), unique=True)
    nickname: Mapped[str] = mapped_column(String(50))
    balance: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[int] = mapped_column(ForeignKey("xl_owner.id"))


UNIQUE_EMAIL = "uq_xl_account_email"
FOREIGN_KEY = "fk_xl_account_owner_id_xl_owner"
CHECK = "ck_xl_account_balance_positive"
SECRET_EMAIL = "secret.person@example.com"


class TranslationAccountRepository(Repository[TranslationAccount, uuid.UUID]):
    pass


async def _sessions(backend: RelationalBackend) -> async_sessionmaker[AsyncSession]:
    await backend.create_tables(TranslationOwner, TranslationAccount)
    factory = async_sessionmaker(backend.create_engine(), expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add(TranslationOwner(id=1))
    return factory


def _account(**overrides: Any) -> TranslationAccount:
    fields: dict[str, Any] = {"email": SECRET_EMAIL, "nickname": "nick", "balance": 10, "owner_id": 1}
    fields.update(overrides)
    return TranslationAccount(**fields)


def _assert_sanitized(error: Exception) -> None:
    message = str(error)
    for leak in ("INSERT", "UPDATE", "[SQL", "parameters", SECRET_EMAIL, "sqlalche.me"):
        assert leak not in message, (leak, message)


async def _failed_save(
    factory: async_sessionmaker[AsyncSession], account: TranslationAccount
) -> DataIntegrityException:
    async with factory() as session:
        with pytest.raises(DataIntegrityException) as raised:
            await TranslationAccountRepository(session=session).save(account)
        await session.rollback()
    return raised.value


async def test_a_duplicate_key_is_a_duplicate_key_exception(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session:
        await TranslationAccountRepository(session=session).save(_account())
        await session.commit()

    error = await _failed_save(factory, _account(nickname="again"))

    assert isinstance(error, DuplicateKeyException) and isinstance(error, ConflictException)
    assert error.code == "INTEGRITY_ERROR"
    assert error.context == {"violation": "unique", "constraint": UNIQUE_EMAIL}
    assert UNIQUE_EMAIL in str(error)
    assert isinstance(error.__cause__, IntegrityError)
    _assert_sanitized(error)


async def test_a_foreign_key_violation(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)

    error = await _failed_save(factory, _account(owner_id=999))

    assert not isinstance(error, DuplicateKeyException)
    assert error.context["violation"] == "foreign_key"
    # SQLite reports "FOREIGN KEY constraint failed" and never which one.
    expected = None if relational_backend.dialect == "sqlite" else FOREIGN_KEY
    assert error.context.get("constraint") == expected
    _assert_sanitized(error)


async def test_a_check_violation(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)

    error = await _failed_save(factory, _account(balance=-5))

    assert error.context == {"violation": "check", "constraint": CHECK}
    _assert_sanitized(error)


async def test_a_not_null_violation(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)

    error = await _failed_save(factory, _account(nickname=None))

    assert error.context["violation"] == "not_null"
    _assert_sanitized(error)


async def test_a_stale_write_is_an_optimistic_locking_failure(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session:
        account = await TranslationAccountRepository(session=session).save(_account())
        await session.commit()

    async with factory() as stale_session, factory() as other:
        stale = await stale_session.get(TranslationAccount, account.id)
        fresh = await other.get(TranslationAccount, account.id)
        assert stale is not None and fresh is not None
        fresh.balance = 20
        await other.commit()

        stale.balance = 30
        with pytest.raises(OptimisticLockingFailureException) as raised:
            await TranslationAccountRepository(session=stale_session).save(stale)
        await stale_session.rollback()

    error = raised.value
    assert isinstance(error, ConcurrencyException) and isinstance(error, ConflictException)
    assert error.code == "OPTIMISTIC_LOCKING_FAILURE"
    # The version check finds no row (StaleDataError); MariaDB's snapshot isolation refuses the stale
    # UPDATE itself ("Record has changed since last read"). Either way, one exception for the service.
    expected_cause = OperationalError if relational_backend.dialect == "mariadb" else StaleDataError
    assert isinstance(error.__cause__, expected_cause)
    assert "xl_account" not in str(error)


# ---------------------------------------------------------------------------------------------------------
# At the unit-of-work boundary: what the commit (or a savepoint release) flushes
# ---------------------------------------------------------------------------------------------------------


@repository
class ManagedAccountRepository(Repository[TranslationAccount, uuid.UUID]):
    pass


@service
class AccountService:
    def __init__(self, accounts: ManagedAccountRepository, sessions: SessionProvider) -> None:
        self._accounts = accounts
        self._sessions = sessions

    @transactional
    async def open_unflushed(self, email: str) -> None:
        session = self._sessions.current()
        assert session is not None
        session.add(_account(email=email))  # flushed by the commit

    @transactional
    async def raise_balance(self, stale: TranslationAccount) -> None:
        session = self._sessions.current()
        assert session is not None
        session.add(stale)
        stale.balance += 1  # flushed by the commit, against the version it was read at

    @transactional
    async def open_two_one_nested(self) -> list[str]:
        outcome: list[str] = []
        await self._accounts.save(_account(email="outer@example.com"))
        try:
            async with TransactionTemplate(propagation=Propagation.NESTED).transaction() as unit:
                unit.resource.add(_account(email="outer@example.com"))  # flushed by the savepoint release
        except DuplicateKeyException as error:
            outcome.append(str(error.context["constraint"]))
        await self._accounts.save(_account(email="second@example.com"))
        return outcome


async def _context(backend: RelationalBackend) -> ApplicationContext:
    await _sessions(backend)
    ctx = ApplicationContext(backend.config())
    ctx.register_bean(ManagedAccountRepository)
    ctx.register_bean(AccountService)
    await ctx.start()
    return ctx


async def test_a_commit_time_duplicate_is_translated_at_the_boundary(relational_backend: RelationalBackend) -> None:
    ctx = await _context(relational_backend)
    try:
        accounts = ctx.get_bean(AccountService)
        await accounts.open_unflushed("first@example.com")
        with pytest.raises(DuplicateKeyException) as raised:
            await accounts.open_unflushed("first@example.com")
        assert raised.value.context["constraint"] == UNIQUE_EMAIL
        assert isinstance(raised.value.__cause__, IntegrityError)
        _assert_sanitized(raised.value)
    finally:
        await ctx.stop()


async def test_a_commit_time_stale_write_is_translated_at_the_boundary(relational_backend: RelationalBackend) -> None:
    ctx = await _context(relational_backend)
    try:
        accounts = ctx.get_bean(ManagedAccountRepository)
        account = await accounts.save(_account())  # auto units: each call commits on its own
        stale = await accounts.find_by_id(account.id)
        fresh = await accounts.find_by_id(account.id)
        assert stale is not None and fresh is not None
        fresh.balance = 99
        await accounts.save(fresh)  # another writer: version 2

        with pytest.raises(OptimisticLockingFailureException) as raised:
            await ctx.get_bean(AccountService).raise_balance(stale)
        assert isinstance(raised.value.__cause__, StaleDataError)
    finally:
        await ctx.stop()


async def test_a_savepoint_release_failure_is_translated(relational_backend: RelationalBackend) -> None:
    ctx = await _context(relational_backend)
    try:
        assert await ctx.get_bean(AccountService).open_two_one_nested() == [UNIQUE_EMAIL]
        emails = sorted(account.email for account in await ctx.get_bean(ManagedAccountRepository).find_all())
        assert emails == ["outer@example.com", "second@example.com"]
    finally:
        await ctx.stop()


@pytest.mark.backends("mariadb")
async def test_mariadb_through_a_mysql_url_translates_the_same(relational_backend: RelationalBackend) -> None:
    """``mysql+asyncmy://`` against MariaDB (dialect ``mysql``) reports the same violations."""
    url = make_url(relational_backend.url).set(drivername="mysql+asyncmy")
    factory = await _sessions(RelationalBackend(relational_backend.lane, url.render_as_string(hide_password=False)))
    try:
        error = await _failed_save(factory, _account(balance=-5))
        assert error.context == {"violation": "check", "constraint": CHECK}
        async with factory() as session:
            await TranslationAccountRepository(session=session).save(_account())
            await session.commit()
        duplicate = await _failed_save(factory, _account(nickname="again"))
        assert isinstance(duplicate, DuplicateKeyException)
        assert duplicate.context["constraint"] == UNIQUE_EMAIL
    finally:
        await factory.kw["bind"].dispose()
