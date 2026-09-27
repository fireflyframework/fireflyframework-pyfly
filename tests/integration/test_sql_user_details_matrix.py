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
"""``SqlUserDetailsService`` on every relational lane (C069).

Documented as working on "any SQLAlchemy AsyncEngine", it created ``username TEXT PRIMARY KEY`` and ``TEXT
... DEFAULT '[]'`` columns (MySQL errors 1170 and 1101) and upserted with ``ON CONFLICT`` (a syntax error on
MySQL and MariaDB). It now uses the framework table ``pyfly_users`` and the dialect's own upsert.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.engine import Connection

from pyfly.data.relational.framework_schema import FrameworkSchemaError
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.security.adapters.sql_user_details import SqlUserDetailsService
from pyfly.security.user_details import UserDetails
from tests.support.backend_matrix import PG, RelationalBackend


async def test_users_round_trip_and_upsert_on_every_backend(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    users = SqlUserDetailsService(lambda: engine)

    await users.save(UserDetails(username="alice", password_hash="{bcrypt}h1", roles=["ADMIN"], permissions=["r"]))
    await users.save(UserDetails(username="alice", password_hash="{bcrypt}h2", roles=["USER"], enabled=False))
    await users.save(UserDetails(username="bob", password_hash="h"))

    alice = await users.load_user_by_username("alice")
    assert alice is not None
    assert (alice.password_hash, alice.roles, alice.permissions, alice.enabled) == ("{bcrypt}h2", ["USER"], [], False)
    bob = await users.load_user_by_username("bob")
    assert bob is not None and bob.enabled is True and bob.roles == []
    assert await users.load_user_by_username("ghost") is None
    await users.delete("bob")
    assert await users.load_user_by_username("bob") is None


async def test_a_custom_table_is_created_on_start(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    users = SqlUserDetailsService(engine, table="wp10a_accounts")
    await users.start()
    await users.stop()

    def names(connection: Connection) -> set[str]:
        return set(inspect(connection).get_table_names())

    async with engine.connect() as connection:
        assert "wp10a_accounts" in await connection.run_sync(names)
    await users.save(UserDetails(username="carol", password_hash="h"))
    assert await users.load_user_by_username("carol") is not None


async def test_without_ddl_a_missing_table_fails_fast(relational_backend: RelationalBackend) -> None:
    users = SqlUserDetailsService(relational_backend.create_engine(), create_table=False)
    with pytest.raises(FrameworkSchemaError, match="table pyfly_users does not exist"):
        await users.start()


async def test_a_user_saved_in_a_transaction_that_rolls_back_is_not_saved(
    relational_backend: RelationalBackend,
) -> None:
    """The store joins the unit of work on its datasource: a sign-up that fails after saving the user leaves
    no user behind."""
    engine = relational_backend.create_engine()
    users = SqlUserDetailsService(engine)
    await users.start()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    with pytest.raises(RuntimeError):
        async with template.transaction():
            await users.save(UserDetails(username="dave", password_hash="h"))
            raise RuntimeError("the welcome e-mail could not be queued")

    assert await users.load_user_by_username("dave") is None


@pytest.mark.backends(PG)
async def test_a_login_lookup_is_one_round_trip_on_postgresql(relational_backend: RelationalBackend) -> None:
    """C093: every login paid BEGIN + SELECT + ROLLBACK."""
    engine = relational_backend.create_engine()
    wire: list[str] = []

    @event.listens_for(engine.sync_engine, "connect")
    def _attach(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.driver_connection.add_query_logger(lambda record: wire.append(record.query))

    users = SqlUserDetailsService(engine)
    await users.start()
    await users.save(UserDetails(username="erin", password_hash="h"))
    wire.clear()

    assert await users.load_user_by_username("erin") is not None
    await users.save(UserDetails(username="erin", password_hash="h2"))

    assert not [query for query in wire if query.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))]
