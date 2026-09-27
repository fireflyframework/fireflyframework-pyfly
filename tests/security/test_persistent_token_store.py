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
"""Persistent OAuth2 token stores: construction and wiring.

Their grant behavior runs against real backends: the SQL store on every relational lane in
``tests/integration/test_oauth2_token_store_matrix.py`` (its sqlite-file lane is in this fast suite), the
Redis store against Redis in ``tests/integration/test_token_store_integration.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pyfly.security.adapters.postgres_token_store import PostgresTokenStore
from pyfly.security.adapters.redis_token_store import RedisTokenStore
from pyfly.security.oauth2.authorization_server import AtomicTokenStore, AuthorizationServer, InMemoryTokenStore


def test_postgres_token_store_rejects_bad_table() -> None:
    with pytest.raises(ValueError, match="table name"):
        PostgresTokenStore(lambda: object(), table="t; DROP TABLE x")
    with pytest.raises(ValueError, match="table name"):
        PostgresTokenStore(lambda: object(), families_table="f; DROP TABLE x")


def test_the_stores_are_atomic_token_stores() -> None:
    assert isinstance(PostgresTokenStore(lambda: object()), AtomicTokenStore)
    assert isinstance(RedisTokenStore(object()), AtomicTokenStore)
    assert isinstance(InMemoryTokenStore(), AtomicTokenStore)


def test_token_store_provider_selection(tmp_path: Path) -> None:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.security.auto_configuration import OAuth2AuthorizationServerAutoConfiguration

    ac = OAuth2AuthorizationServerAutoConfiguration()
    assert isinstance(ac._build_token_store(Config({}), Container(), 86400), InMemoryTokenStore)
    pg_cfg = Config(
        {
            "pyfly": {
                "data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'as.db'}"}},
                "security": {"oauth2": {"token-store": {"provider": "postgres"}}},
            }
        }
    )
    assert isinstance(ac._build_token_store(pg_cfg, Container(), 86400), PostgresTokenStore)


@pytest.mark.asyncio
async def test_the_sql_store_is_on_the_contexts_datasource(tmp_path: Path) -> None:
    """The store resolves its datasource in the context's DataSourceRegistry (the primary unless
    ``token-store.datasource`` or ``token-store.url`` names another), creates its tables when the schema
    strategy allows it (the server starts it), and grants tokens."""
    from pyfly.container.container import Container
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.security.auto_configuration import OAuth2AuthorizationServerAutoConfiguration
    from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository

    config = Config(
        {
            "pyfly": {
                "data": {"relational": {"enabled": True, "url": f"sqlite+aiosqlite:///{tmp_path / 'as.db'}"}},
                "security": {"oauth2": {"token-store": {"provider": "postgres"}}},
            }
        }
    )
    ctx = ApplicationContext(config)
    await ctx.start()
    try:
        container: Any = ctx.get_bean(Container)
        store = OAuth2AuthorizationServerAutoConfiguration()._build_token_store(config, container, 86400)
        assert isinstance(store, PostgresTokenStore)
        assert store.engine is ctx.get_bean(DataSourceRegistry).engine()
        server = AuthorizationServer(
            secret="s" * 48,
            client_repository=InMemoryClientRegistrationRepository(
                ClientRegistration(
                    registration_id="svc",
                    client_id="svc",
                    client_secret="svc-secret",
                    authorization_grant_type="client_credentials",
                    scopes=["read"],
                )
            ),
            token_store=store,
        )
        await server.start()
        issued = await server.token(grant_type="client_credentials", client_id="svc", client_secret="svc-secret")
        assert (await server.introspect(issued["refresh_token"]))["active"] is True
        await server.stop()
    finally:
        await ctx.stop()
