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
"""Integration test: Postgres SessionRegistry against a real Postgres (v26.06.68)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pyfly.testing import postgres_container, pyfly_config_for, requires_docker


@pytest.fixture
def pg_url() -> Iterator[str]:
    with postgres_container() as container:
        yield pyfly_config_for(container)["pyfly.data.relational.url"]


@requires_docker
@pytest.mark.asyncio
async def test_postgres_session_registry_against_real_postgres(pg_url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry

    engine = create_async_engine(pg_url)
    try:
        reg = PostgresSessionRegistry(lambda: engine)
        await reg.register("alice", "s2", 2.0)
        await reg.register("alice", "s1", 1.0)  # older score, inserted second
        assert await reg.count("alice") == 2
        assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s1", "s2"]  # oldest-first

        await reg.register("alice", "s2", 9.0)  # upsert (same session_id) must not duplicate
        assert await reg.count("alice") == 2

        await reg.deregister("alice", "s1")
        assert await reg.count("alice") == 1
        assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s2"]
    finally:
        await engine.dispose()
