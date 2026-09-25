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
"""Read/write datasource routing (v26.06.29) — the AbstractRoutingDataSource equivalent."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyfly.core.config import Config
from pyfly.data.relational.routing import RoutingSessionFactory, is_read_only, read_only


def test_routes_to_replica_only_inside_read_only() -> None:
    factory = RoutingSessionFactory(lambda: "PRIMARY", lambda: "REPLICA")
    assert factory.has_replica is True
    assert factory() == "PRIMARY"  # default: read/write -> primary
    with read_only():
        assert factory() == "REPLICA"  # read-only -> replica
    assert factory() == "PRIMARY"  # context restored


def test_no_replica_always_primary() -> None:
    factory = RoutingSessionFactory(lambda: "PRIMARY", None)
    assert factory.has_replica is False
    with read_only():
        assert factory() == "PRIMARY"  # no replica configured -> primary even when read-only
    assert factory.replica() == "PRIMARY"  # explicit replica() falls back to primary


def test_explicit_accessors_and_nesting() -> None:
    factory = RoutingSessionFactory(lambda: "PRIMARY", lambda: "REPLICA")
    assert factory.primary() == "PRIMARY"
    assert factory.replica() == "REPLICA"
    assert is_read_only() is False
    with read_only():
        assert is_read_only() is True
        with read_only():
            assert is_read_only() is True
        assert is_read_only() is True  # inner exit keeps the outer read-only state
    assert is_read_only() is False


@pytest.mark.asyncio
async def test_bean_builds_replica_when_configured(tmp_path: Path) -> None:
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    # The routing factory is a view over the registry: the primary's session factory and its replica's.
    cfg = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}",
                        "read-replica": {"url": f"sqlite+aiosqlite:///{tmp_path / 'replica.db'}"},
                    }
                }
            }
        }
    )
    registry = DataSourceRegistry.for_config(cfg)
    try:
        auto = RelationalAutoConfiguration()
        primary = auto.async_session_factory(auto.async_engine(cfg))
        factory = auto.routing_session_factory(primary, cfg)
        assert factory.has_replica is True
        replica = registry.replica()
        assert replica is not None
        assert factory._primary is registry.primary.sessionmaker
        assert factory._replica is replica.sessionmaker
    finally:
        await registry.close()

    # A session factory that is not the registry's routes to itself only.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        foreign = async_sessionmaker(engine, expire_on_commit=False)
        no_replica = RelationalAutoConfiguration().routing_session_factory(foreign, Config({}))
        assert no_replica.has_replica is False
    finally:
        await engine.dispose()
