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
"""Primary-authoritative read-only units against deliberately divergent replicas."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Integer, String, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data import Isolation, Propagation, transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy import Repository, SessionProvider
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    TransactionTemplate,
    current_unit_of_work,
    is_current_transaction_read_only,
)


class AuthorityGrant(Base):
    __tablename__ = "authority_grant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(64))


@repository
class AuthorityGrantRepository(Repository[AuthorityGrant, int]):
    pass


@pytest.fixture
async def authority_app(tmp_path: Path) -> AsyncIterator[ApplicationContext]:
    urls = {name: f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}" for name in ("primary", "replica", "reporting")}
    for name, url in urls.items():
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all, tables=[AuthorityGrant.__table__])
                await connection.execute(
                    AuthorityGrant.__table__.insert(),
                    [{"id": 1, "state": "revoked" if name == "primary" else f"granted-{name}"}],
                )
        finally:
            await engine.dispose()
    config: dict[str, Any] = {
        "enabled": "true",
        "url": urls["primary"],
        "ddl-auto": "none",
        "read-replica": {"url": urls["replica"]},
        "datasources": {"reporting": {"url": urls["reporting"]}},
    }
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": config}}}))
    ctx.register_bean(RelationalAutoConfiguration)
    ctx.register_bean(AuthorityGrantRepository)
    await ctx.start()
    try:
        yield ctx
        registry = ctx.get_bean(DataSourceRegistry)
        assert all(ds.engine.sync_engine.pool.checkedout() == 0 for ds in registry.all_datasources())
    finally:
        await ctx.stop()


async def _state(session: AsyncSession) -> str:
    return str(await session.scalar(select(AuthorityGrant.state).where(AuthorityGrant.id == 1)))


async def test_template_primary_read_observes_revoke_and_new_grant(authority_app: ApplicationContext) -> None:
    template = TransactionTemplate("primary", read_only=True, isolation=Isolation.SERIALIZABLE)
    async with template.transaction() as replica:
        assert replica is not None
        assert await _state(replica.resource) == "granted-replica"
    async with template.transaction(read_from_primary=True) as primary:
        assert primary is not None
        assert primary.read_from_primary is True
        assert primary.read_only and primary.isolation is Isolation.SERIALIZABLE
        assert await _state(primary.resource) == "revoked"
        connection = await primary.resource.connection()
        assert await connection.get_isolation_level() == "SERIALIZABLE"
    async with TransactionTemplate("primary", read_from_primary=True).transaction() as writer:
        assert writer is not None
        await writer.resource.execute(update(AuthorityGrant).values(state="new-grant"))
    async with template.transaction(read_from_primary=True) as fresh:
        assert fresh is not None and fresh.resource is not primary.resource
        assert await _state(fresh.resource) == "new-grant"
    async with template.transaction() as still_stale:
        assert still_stale is not None and still_stale.read_from_primary is False
        assert await _state(still_stale.resource) == "granted-replica"


@pytest.mark.parametrize("participant_primary", [False, True])
async def test_decorator_required_repository_and_session_participants_keep_primary(
    authority_app: ApplicationContext, participant_primary: bool
) -> None:
    items = authority_app.get_bean(AuthorityGrantRepository)
    sessions = authority_app.get_bean(SessionProvider)
    shared_session = authority_app.get_bean(AsyncSession)

    @transactional(datasource="primary", read_only=True, read_from_primary=participant_primary)
    async def participant() -> str:
        unit = current_unit_of_work("primary")
        assert unit is not None and sessions.current() is unit.resource
        assert items._session is unit.resource
        assert shared_session._pyfly_delegate() is unit.resource
        assert await _state(shared_session) == "revoked"
        row = await items.find_by_id(1)
        assert row is not None
        return row.state

    @transactional(
        datasource="primary",
        propagation=Propagation.REQUIRES_NEW,
        isolation=Isolation.SERIALIZABLE,
        read_only=True,
        read_from_primary=True,
    )
    async def snapshot() -> str:
        unit = current_unit_of_work("primary")
        assert unit is not None and unit.read_from_primary
        assert unit.manager is TransactionTemplate("primary").manager()
        assert is_current_transaction_read_only() and unit.resource.info["read_only"]
        assert await participant() == "revoked"
        assert current_unit_of_work("primary") is unit
        return await _state(unit.resource)

    async with TransactionTemplate("primary", read_only=True).transaction() as replica:
        assert replica is not None
        assert await _state(replica.resource) == "granted-replica"
        assert await snapshot() == "revoked"
        assert current_unit_of_work("primary") is replica
        assert await _state(replica.resource) == "granted-replica"


@pytest.mark.parametrize("write", ["orm", "core"])
async def test_primary_read_still_refuses_writes(authority_app: ApplicationContext, write: str) -> None:
    with pytest.raises(IllegalTransactionStateError, match="read-only"):
        async with TransactionTemplate("primary", read_only=True, read_from_primary=True).transaction() as unit:
            assert unit is not None
            if write == "orm":
                unit.resource.add(AuthorityGrant(id=2, state="forbidden"))
                await unit.resource.flush()
            else:
                await unit.resource.execute(update(AuthorityGrant).values(state="forbidden"))
    async with TransactionTemplate("primary", read_only=True, read_from_primary=True).transaction() as unit:
        assert unit is not None
        assert await _state(unit.resource) == "revoked"


async def test_primary_snapshot_does_not_wait_for_suspended_sqlite_writer(authority_app: ApplicationContext) -> None:
    async with TransactionTemplate("primary").transaction() as writer:
        assert writer is not None
        await writer.resource.execute(update(AuthorityGrant).values(state="uncommitted"))
        async with asyncio.timeout(1):
            async with TransactionTemplate(
                "primary",
                propagation=Propagation.REQUIRES_NEW,
                isolation=Isolation.SERIALIZABLE,
                read_only=True,
                read_from_primary=True,
            ).transaction() as snapshot:
                assert snapshot is not None and snapshot.resource is not writer.resource
                assert await _state(snapshot.resource) == "revoked"
        assert current_unit_of_work("primary") is writer
        assert await _state(writer.resource) == "uncommitted"


@pytest.mark.parametrize(
    "propagation", [Propagation.REQUIRED, Propagation.SUPPORTS, Propagation.MANDATORY, Propagation.NESTED]
)
async def test_primary_demand_cannot_join_replica(authority_app: ApplicationContext, propagation: Propagation) -> None:
    async with TransactionTemplate("primary", read_only=True).transaction() as replica:
        with pytest.raises(IllegalTransactionStateError, match="read_from_primary.*REQUIRES_NEW"):
            async with TransactionTemplate("primary", propagation=propagation, read_from_primary=True).transaction():
                pytest.fail("a primary-authoritative body cannot run on the replica")
        assert current_unit_of_work("primary") is replica
        assert replica is not None and await _state(replica.resource) == "granted-replica"


async def test_primary_preference_preserves_named_datasource(authority_app: ApplicationContext) -> None:
    async with (
        TransactionTemplate("primary", read_only=True, read_from_primary=True).transaction() as primary,
        TransactionTemplate("reporting", read_only=True, read_from_primary=True).transaction() as reporting,
    ):
        assert primary is not None and reporting is not None
        assert reporting.datasource == "reporting" and reporting.manager is not primary.manager
        assert await _state(reporting.resource) == "granted-reporting"
        assert current_unit_of_work("primary") is primary
        assert await _state(primary.resource) == "revoked"


async def test_custom_session_factory_preserves_options_on_primary(authority_app: ApplicationContext) -> None:
    from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager

    primary = authority_app.get_bean(DataSourceRegistry).primary
    factory = async_sessionmaker(primary.engine, expire_on_commit=True, autoflush=False)
    manager = SqlAlchemyTransactionManager(primary, sessionmaker=factory, name="custom")
    async with TransactionTemplate(manager, read_only=True).transaction() as replica:
        assert replica is not None and not replica.read_from_primary
        assert await _state(replica.resource) == "granted-replica"
    async with TransactionTemplate(manager, read_only=True, read_from_primary=True).transaction() as unit:
        assert unit is not None
        assert await _state(unit.resource) == "revoked"
        assert unit.resource.autoflush is False and unit.resource.sync_session.expire_on_commit is True


async def test_class_and_template_allow_explicit_replica_override(authority_app: ApplicationContext) -> None:
    @transactional(datasource="primary", read_only=True, read_from_primary=True)
    class Reader:
        async def primary(self) -> str:
            unit = current_unit_of_work("primary")
            assert unit is not None
            return await _state(unit.resource)

        @transactional(read_from_primary=False)
        async def replica(self) -> str:
            unit = current_unit_of_work("primary")
            assert unit is not None
            return await _state(unit.resource)

    reader = Reader()
    assert await reader.primary() == "revoked"
    assert await reader.replica() == "granted-replica"
    template = TransactionTemplate("primary", read_only=True, read_from_primary=True)
    async with template.transaction(read_from_primary=False) as replica:
        assert replica is not None and not replica.read_from_primary
        assert await _state(replica.resource) == "granted-replica"
    assert template.definition.read_from_primary is True


@pytest.mark.parametrize("resource", ["factory", "engine", "datasource"])
async def test_registered_replica_resource_resolves_to_primary(
    authority_app: ApplicationContext, resource: str
) -> None:
    replica = authority_app.get_bean(DataSourceRegistry).replica()
    assert replica is not None
    target = {"factory": replica.sessionmaker, "engine": replica.engine, "datasource": replica}[resource]
    template = TransactionTemplate(target, read_only=True, read_from_primary=True)
    assert template.manager() is TransactionTemplate("primary").manager()
    async with template.transaction() as unit:
        assert unit is not None and unit.read_from_primary
        assert await _state(unit.resource) == "revoked"


@pytest.mark.parametrize("bound_datasource", [False, True])
async def test_custom_replica_factory_cannot_claim_primary(
    authority_app: ApplicationContext, bound_datasource: bool
) -> None:
    from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
    from pyfly.data.transaction.template import auto_unit

    replica = authority_app.get_bean(DataSourceRegistry).replica()
    assert replica is not None
    factory = async_sessionmaker(replica.engine, expire_on_commit=False)
    target = (
        SqlAlchemyTransactionManager(replica, sessionmaker=factory, name="custom-replica")
        if bound_datasource
        else factory
    )
    async with TransactionTemplate(target, read_only=True).transaction() as unit:
        assert unit is not None
        assert await _state(unit.resource) == "granted-replica"
        assert unit.read_from_primary is False
    async with auto_unit(TransactionTemplate(target).manager(), read_only=True) as auto:
        assert auto.read_from_primary is False
        assert await _state(auto.resource) == "granted-replica"
    with pytest.raises(IllegalTransactionStateError, match="read_from_primary.*replica"):
        async with TransactionTemplate(target, read_only=True, read_from_primary=True).transaction():
            pytest.fail("a factory bound directly to a replica cannot claim primary authority")


async def test_routing_factory_sessions_remain_independently_owned(authority_app: ApplicationContext) -> None:
    from pyfly.data.relational.routing import RoutingSessionFactory

    factory = authority_app.get_bean(RoutingSessionFactory)
    async with TransactionTemplate("primary", read_only=True, read_from_primary=True).transaction() as unit:
        assert unit is not None and unit.read_from_primary
        assert await _state(unit.resource) == "revoked"
        async with factory() as independent:
            assert independent is not unit.resource
            assert await _state(independent) == "granted-replica"
        assert current_unit_of_work("primary") is unit


async def test_primary_factory_override_on_replica_rejects_authority(authority_app: ApplicationContext) -> None:
    from pyfly.data.relational.sqlalchemy.transaction_manager import (
        bind_primary_session_factory,
        unbind_primary_session_factory,
    )

    registry = authority_app.get_bean(DataSourceRegistry)
    replica = registry.replica()
    assert replica is not None
    factory = async_sessionmaker(replica.engine, expire_on_commit=False)
    manager = bind_primary_session_factory(registry, factory)
    try:
        async with TransactionTemplate("primary", read_only=True).transaction() as unit:
            assert unit is not None and unit.manager is manager
            assert unit.read_from_primary is False
            assert await _state(unit.resource) == "granted-replica"
        with pytest.raises(IllegalTransactionStateError, match="read_from_primary.*replica"):
            async with TransactionTemplate("primary", read_only=True, read_from_primary=True).transaction():
                pytest.fail("a primary bean on a replica cannot claim authority")
        async with TransactionTemplate(
            registry.primary, read_only=True, read_from_primary=True
        ).transaction() as actual:
            assert actual is not None and actual.read_from_primary
            assert await _state(actual.resource) == "revoked"
    finally:
        unbind_primary_session_factory(registry, manager)
