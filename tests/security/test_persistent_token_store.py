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
"""Persistent OAuth2 token stores (v26.06.69) — Redis + Postgres adapters, unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.security.adapters.postgres_token_store import PostgresTokenStore
from pyfly.security.adapters.redis_token_store import RedisTokenStore


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


@pytest.mark.asyncio
async def test_redis_token_store_roundtrip_with_ttl() -> None:
    redis = _FakeRedis()
    store = RedisTokenStore(redis, ttl=900)
    await store.store("tok1", {"sub": "alice", "scope": "read"})
    assert await store.find("tok1") == {"sub": "alice", "scope": "read"}
    assert redis.ttls["pyfly:oauth2:token:tok1"] == 900  # refresh-token TTL applied
    await store.revoke("tok1")
    assert await store.find("tok1") is None


@pytest.mark.asyncio
async def test_redis_token_store_decodes_bytes() -> None:
    redis = _FakeRedis()
    store = RedisTokenStore(redis)
    await store.store("t", {"a": 1})
    redis.store["pyfly:oauth2:token:t"] = redis.store["pyfly:oauth2:token:t"].encode("utf-8")  # type: ignore[assignment]
    assert await store.find("t") == {"a": 1}


def test_postgres_token_store_rejects_bad_table() -> None:
    with pytest.raises(ValueError, match="table name"):
        PostgresTokenStore(lambda: object(), table="t; DROP TABLE x")


def test_token_store_provider_selection() -> None:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.security.auto_configuration import OAuth2AuthorizationServerAutoConfiguration
    from pyfly.security.oauth2.authorization_server import InMemoryTokenStore

    ac = OAuth2AuthorizationServerAutoConfiguration()
    assert isinstance(ac._build_token_store(Config({}), Container(), 86400), InMemoryTokenStore)
    pg_cfg = Config({"pyfly": {"security": {"oauth2": {"token-store": {"provider": "postgres"}}}}})
    assert isinstance(ac._build_token_store(pg_cfg, Container(), 86400), PostgresTokenStore)
