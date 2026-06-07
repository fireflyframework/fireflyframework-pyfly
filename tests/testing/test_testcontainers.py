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
"""Testcontainers integration (v26.06.31): @ServiceConnection-style config mapping,
graceful skip without Docker, and clear error without the extra.

The connection-mapping logic is tested with duck-typed fakes (no Docker needed); real
container startup is covered by the @requires_docker skip path.
"""

from __future__ import annotations

import pytest

from pyfly.testing.testcontainers import (
    _load,
    _nest,
    is_docker_available,
    pyfly_config,
    pyfly_config_for,
    requires_docker,
)


class _FakePostgres:
    def get_connection_url(self) -> str:
        return "postgresql+psycopg2://u:p@h:5432/test"


class _FakeMySql:
    def get_connection_url(self) -> str:
        return "mysql+pymysql://u:p@h:3306/test"


class _FakeRedis:
    def get_container_host_ip(self) -> str:
        return "127.0.0.1"

    def get_exposed_port(self, port: int) -> int:
        return 55001


class _FakeMongoDb:
    def get_connection_url(self) -> str:
        return "mongodb://h:27017"


class _FakeKafka:
    def get_bootstrap_server(self) -> str:
        return "127.0.0.1:55002"


def test_postgres_mapping_to_async_url() -> None:
    assert pyfly_config_for(_FakePostgres()) == {"pyfly.data.relational.url": "postgresql+asyncpg://u:p@h:5432/test"}


def test_mysql_mapping_to_async_url() -> None:
    assert pyfly_config_for(_FakeMySql()) == {"pyfly.data.relational.url": "mysql+aiomysql://u:p@h:3306/test"}


def test_redis_mapping_to_cache_and_session() -> None:
    cfg = pyfly_config_for(_FakeRedis())
    assert cfg["pyfly.cache.redis.url"] == "redis://127.0.0.1:55001/0"
    assert cfg["pyfly.session.redis.url"] == "redis://127.0.0.1:55001/0"


def test_mongo_and_kafka_mappings() -> None:
    assert pyfly_config_for(_FakeMongoDb()) == {"pyfly.data.document.uri": "mongodb://h:27017"}
    assert pyfly_config_for(_FakeKafka()) == {"pyfly.eda.kafka.bootstrap-servers": "127.0.0.1:55002"}


def test_unmapped_container_raises() -> None:
    class Other:
        pass

    with pytest.raises(ValueError):
        pyfly_config_for(Other())


def test_pyfly_config_builds_nested_config() -> None:
    cfg = pyfly_config(_FakePostgres(), _FakeRedis())
    assert cfg.get("pyfly.data.relational.url") == "postgresql+asyncpg://u:p@h:5432/test"
    assert cfg.get("pyfly.cache.redis.url") == "redis://127.0.0.1:55001/0"


def test_nest_flat_keys() -> None:
    assert _nest({"a.b.c": 1, "a.b.d": 2, "x": 3}) == {"a": {"b": {"c": 1, "d": 2}}, "x": 3}


def test_is_docker_available_returns_bool() -> None:
    assert isinstance(is_docker_available(), bool)


def test_load_missing_module_raises_clear_install_hint() -> None:
    # _load surfaces the extra/install hint when a backing module is unavailable —
    # independent of whether the testcontainers extra happens to be installed.
    with pytest.raises(RuntimeError, match=r"pyfly\[testcontainers\]"):
        _load("testcontainers._pyfly_nonexistent_module", "Nope")


def test_requires_docker_applies_skipif_mark() -> None:
    # Robust across environments (Docker present or not): the decorator must attach a
    # skipif mark so the test skips wherever Docker is unavailable.
    @requires_docker
    def dummy() -> None:
        pass

    marks = getattr(dummy, "pytestmark", [])
    assert any(getattr(mark, "name", "") == "skipif" for mark in marks)
