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
"""The database ``@scheduled`` locks: provider selection, and the lease lock on a SQLite file.

Every backend runs in ``tests/integration/test_postgres_lock_integration.py``: the lease table on SQLite,
PostgreSQL, MySQL and MariaDB, and the PostgreSQL advisory accelerator against a real server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyfly.container.container import Container
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceConfigurationError, DataSourceRegistry
from pyfly.scheduling.adapters.lease_lock import LeaseLock
from pyfly.scheduling.adapters.postgres_lock import PostgresAdvisoryLock
from pyfly.scheduling.auto_configuration import SchedulingAutoConfiguration
from pyfly.scheduling.lock import DistributedLock


def test_key_is_deterministic_signed_64bit() -> None:
    k = PostgresAdvisoryLock._key("job")
    assert PostgresAdvisoryLock._key("job") == k  # deterministic (not salted hash())
    assert -(2**63) <= k < 2**63
    assert PostgresAdvisoryLock._key("other-job") != k


def test_both_database_locks_satisfy_the_distributed_lock_protocol(tmp_path: Path) -> None:
    assert isinstance(PostgresAdvisoryLock(lambda: None), DistributedLock)
    assert isinstance(LeaseLock(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"), DistributedLock)


def _config(tmp_path: Path, **lock: object) -> Config:
    return Config(
        {
            "pyfly": {
                "scheduling": {"lock": lock},
                "data": {
                    "relational": {
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                        "datasources": {"locks": {"url": f"sqlite+aiosqlite:///{tmp_path / 'locks.db'}"}},
                    }
                },
            }
        }
    )


@pytest.mark.parametrize("provider", ["database", "postgres"])
async def test_a_database_provider_is_the_lease_table_on_the_primary(provider: str, tmp_path: Path) -> None:
    config = _config(tmp_path, provider=provider)
    registry = DataSourceRegistry.for_config(config)
    try:
        lock = SchedulingAutoConfiguration().distributed_lock(config, Container())
        assert isinstance(lock, LeaseLock)
        await lock.start()
        assert await lock.try_acquire("job", 30.0) is True
        assert await LeaseLock(registry.primary).try_acquire("job", 30.0) is False  # the primary's table
        await lock.release("job")
    finally:
        await registry.close()


async def test_the_lock_datasource_key_names_where_the_leases_live(tmp_path: Path) -> None:
    config = _config(tmp_path, provider="database", datasource="locks")
    registry = DataSourceRegistry.for_config(config)
    try:
        lock = SchedulingAutoConfiguration().distributed_lock(config, Container())
        assert isinstance(lock, LeaseLock)
        await lock.start()
        assert await lock.try_acquire("job", 30.0) is True
        assert await LeaseLock(registry.get("locks")).try_acquire("job", 30.0) is False
        assert await LeaseLock(registry.primary).try_acquire("job", 30.0) is True  # another database, free
    finally:
        await registry.close()


async def test_the_context_registry_bean_is_the_one_the_lock_runs_on(tmp_path: Path) -> None:
    config = Config({"pyfly": {"scheduling": {"lock": {"provider": "database"}}}})
    own = DataSourceRegistry(
        Config({"pyfly": {"data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"}}}})
    )
    container = Container()
    container.register_instance(DataSourceRegistry, own)
    try:
        lock = SchedulingAutoConfiguration().distributed_lock(config, container)
        assert isinstance(lock, LeaseLock)
        await lock.start()
        assert await lock.try_acquire("job", 30.0) is True
        assert await LeaseLock(own.primary).try_acquire("job", 30.0) is False
    finally:
        await own.close()


async def test_the_advisory_accelerator_is_opt_in_on_postgresql() -> None:
    config = Config(
        {
            "pyfly": {
                "scheduling": {"lock": {"provider": "postgres", "postgres": {"advisory": "true"}}},
                "data": {"relational": {"url": "postgresql+asyncpg://localhost:5432/app"}},
            }
        }
    )
    registry = DataSourceRegistry.for_config(config)
    try:
        lock = SchedulingAutoConfiguration().distributed_lock(config, Container())
        assert isinstance(lock, PostgresAdvisoryLock)  # built without connecting
    finally:
        await registry.close()


async def test_the_advisory_accelerator_refuses_a_datasource_that_is_not_postgresql(tmp_path: Path) -> None:
    config = _config(tmp_path, provider="postgres", postgres={"advisory": "true"})
    registry = DataSourceRegistry.for_config(config)
    try:
        with pytest.raises(DataSourceConfigurationError, match="needs a PostgreSQL datasource"):
            SchedulingAutoConfiguration().distributed_lock(config, Container())
    finally:
        await registry.close()
