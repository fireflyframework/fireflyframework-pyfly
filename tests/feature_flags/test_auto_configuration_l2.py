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
"""The store, HTTP source and sync server are wired through the application context."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider

from pyfly.container.bean import bean
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.container.stereotypes import configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.feature_flags.client import FeatureFlags
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.server import FlagSyncServer
from pyfly.feature_flags.store.memory import MemoryFlagStore
from pyfly.feature_flags.store.ports import FlagStore
from pyfly.feature_flags.store.sqlalchemy import SqlAlchemyFlagStore
from pyfly.feature_flags.store.writer import FlagStoreWriter
from pyfly.web.adapters.starlette.app import create_app
from tests.feature_flags.support import bool_flag, wait_until


def _config(section: dict[str, Any], *, database: Path | None = None, domain: str = "") -> Config:
    data: dict[str, Any] = {"feature-flags": {"enabled": "true", "openfeature": {"domain": domain}, **section}}
    if database is not None:
        data["data"] = {"relational": {"enabled": "true", "url": f"sqlite+aiosqlite:///{database}"}}
    return Config({"pyfly": data})


async def _started(config: Config, *beans: type) -> ApplicationContext:
    context = ApplicationContext(config)
    for bean_class in beans:
        context.register_bean(bean_class)
    await context.start()
    return context


async def test_the_memory_store_is_a_writable_layer() -> None:
    context = await _started(
        _config({"flags": {"a": False}, "sources": {"store": {"enabled": True, "driver": "memory"}}})
    )
    assert isinstance(context.get_bean(FlagStore), MemoryFlagStore)
    await context.get_bean(FlagStoreWriter).put("a", bool_flag("on"), actor="ops")
    assert context.get_bean(FeatureFlags).is_enabled("a") is True
    assert [source.name for source in context.get_bean(FlagRegistry).sources()] == ["config", "store"]
    await context.stop()


async def test_without_the_store_there_is_no_writer() -> None:
    context = await _started(_config({"flags": {"a": True}}))
    with pytest.raises(NoSuchBeanError):
        context.get_bean(FlagStoreWriter)
    await context.stop()


async def test_the_database_store_creates_its_tables_on_the_primary_datasource(tmp_path: Path) -> None:
    context = await _started(_config({"sources": {"store": {"enabled": True}}}, database=tmp_path / "app.db"))
    store = context.get_bean(FlagStore)
    assert isinstance(store, SqlAlchemyFlagStore)
    await context.get_bean(FlagStoreWriter).put("db-flag", bool_flag(), actor="ops")
    assert context.get_bean(FeatureFlags).is_enabled("db-flag") is True
    assert (await store.get("db-flag")).version == 1  # type: ignore[union-attr]
    await context.stop()


async def test_a_write_in_one_application_reaches_another_sharing_the_database(tmp_path: Path) -> None:
    database = tmp_path / "shared.db"
    store = {"sources": {"store": {"enabled": True, "refresh-interval": "50ms"}}}
    writer_app = await _started(_config(store, database=database, domain="writer"))
    reader_app = await _started(_config(store, database=database, domain="reader"))
    reader = reader_app.get_bean(FeatureFlags)
    assert reader.is_enabled("shared") is False
    await writer_app.get_bean(FlagStoreWriter).put("shared", bool_flag(), actor="ops")
    await wait_until(lambda: reader.is_enabled("shared"))
    await reader_app.stop()
    await writer_app.stop()


async def test_an_unreachable_http_source_never_fails_startup() -> None:
    http = {"enabled": True, "url": "http://127.0.0.1:9/feature-flags/flagd.json", "timeout": "200ms"}
    context = await _started(_config({"flags": {"a": True}, "sources": {"http": http}}))
    statuses = {source.name: source.status for source in context.get_bean(FlagRegistry).sources()}
    assert statuses == {"config": "UP", "http": "DOWN"}
    assert context.get_bean(FeatureFlags).is_enabled("a") is True
    await context.stop()


@configuration
class OwnStoreConfiguration:
    @bean
    def own_flag_store(self) -> FlagStore:
        return MemoryFlagStore()


async def test_an_application_store_bean_replaces_the_configured_one() -> None:
    context = await _started(_config({"sources": {"store": {"enabled": True}}}), OwnStoreConfiguration)
    assert isinstance(context.get_bean(FlagStore), MemoryFlagStore)
    assert context.get_bean(FlagStoreWriter).store is context.get_bean(FlagStore)
    await context.stop()


@configuration
class OwnProviderConfiguration:
    @bean
    def own_provider(self) -> InMemoryProvider:
        return InMemoryProvider({"ext": InMemoryFlag("on", {"on": True, "off": False})})


async def test_an_external_provider_disables_the_builtin_store() -> None:
    context = await _started(
        _config({"sources": {"store": {"enabled": True, "driver": "memory"}}}), OwnProviderConfiguration
    )
    assert context.get_bean(FeatureFlags).is_enabled("ext") is True
    for bean_type in (FlagStore, FlagStoreWriter, FlagRegistry):
        with pytest.raises(NoSuchBeanError):
            context.get_bean(bean_type)
    await context.stop()


async def test_an_external_provider_does_not_remove_an_application_store() -> None:
    context = await _started(
        _config({"sources": {"store": {"enabled": True, "driver": "memory"}}}),
        OwnProviderConfiguration,
        OwnStoreConfiguration,
    )
    assert isinstance(context.get_bean(FlagStore), MemoryFlagStore)
    with pytest.raises(NoSuchBeanError):
        context.get_bean(FlagStoreWriter)
    await context.stop()


async def test_the_sync_route_is_mounted_under_the_canonical_boot_order() -> None:
    context = ApplicationContext(_config({"flags": {"a": True}, "server": {"enabled": True, "token": "t"}}))

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = create_app(context=context, lifespan=lifespan, actuator_enabled=False, docs_enabled=False)
    async with app.router.lifespan_context(app):
        assert isinstance(context.get_bean(FlagSyncServer), FlagSyncServer)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/feature-flags/flagd.json", headers={"Authorization": "Bearer t"})
            assert response.status_code == 200 and set(response.json()["flags"]) == {"a"}
            assert (await client.get("/feature-flags/flagd.json")).status_code == 401
