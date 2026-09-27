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
"""Session auto-configuration: the SQL session store, the registry's datasource and the store it checks
(WP10b: C076, C154)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import pytest
from pyfly.session.adapters.sql_session_store import SqlSessionStore

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
from pyfly.session.concurrency import SessionConcurrencyController
from pyfly.session.ports.outbound import SessionStore


def _config(tmp_path: Path, **session: Any) -> Config:
    return Config(
        {
            "pyfly": {
                "data": {"relational": {"enabled": True, "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"}},
                "session": {"enabled": True, **session},
            }
        }
    )


@pytest.mark.asyncio
async def test_store_postgres_is_the_sql_session_store_on_the_primary(tmp_path: Path) -> None:
    ctx = ApplicationContext(
        _config(tmp_path, store="postgres", concurrency={"enabled": True, "registry": "postgres", "max-sessions": 1})
    )
    await ctx.start()
    try:
        store = ctx.get_bean(SessionStore)
        assert isinstance(store, SqlSessionStore)
        assert store.engine is ctx.get_bean(DataSourceRegistry).engine()
        controller = ctx.get_bean(SessionConcurrencyController)
        assert isinstance(controller._registry, PostgresSessionRegistry)
        assert controller.session_store is store

        await store.save("s1", {"user": "ann"}, ttl=60)
        assert await controller.on_login("ann", "s1", time.time())
        await store.save("s2", {"user": "ann"}, ttl=60)
        assert await controller.on_login("ann", "s2", time.time())  # evict-oldest: s1 goes
        assert await store.get("s1") is None
    finally:
        await ctx.stop()


def test_a_cross_process_registry_beside_a_process_local_store_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C154: the registry is shared, the sessions are not: evicting a session another instance holds leaves it
    usable there."""
    from pyfly.container.container import Container
    from pyfly.session.auto_configuration import SessionConcurrencyAutoConfiguration

    config = _config(tmp_path, concurrency={"registry": "postgres"})
    with caplog.at_level(logging.WARNING, logger="pyfly.session.auto_configuration"):
        SessionConcurrencyAutoConfiguration().session_concurrency_controller(
            config, InMemorySessionStore(), Container()
        )

    assert any(record.getMessage().startswith("session_registry_not_shared") for record in caplog.records)


@pytest.mark.parametrize("key", ["store", "concurrency.registry"])
def test_an_unknown_backend_fails_fast(tmp_path: Path, key: str) -> None:
    """``store: jdbc`` silently became the in-memory store."""
    from pyfly.container.container import Container
    from pyfly.session.auto_configuration import SessionConcurrencyAutoConfiguration, SessionStoreAutoConfiguration

    if key == "store":
        with pytest.raises(ValueError, match="pyfly.session.store"):
            SessionStoreAutoConfiguration().session_store(_config(tmp_path, store="jdbc"), Container())
    else:
        with pytest.raises(ValueError, match="pyfly.session.concurrency.registry"):
            SessionConcurrencyAutoConfiguration().session_concurrency_controller(
                _config(tmp_path, concurrency={"registry": "jdbc"}), InMemorySessionStore(), Container()
            )
