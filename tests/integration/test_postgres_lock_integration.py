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
"""Integration test: Postgres advisory-lock DistributedLock against a real Postgres (v26.06.66)."""

from __future__ import annotations

import pytest

from pyfly.testing import requires_docker  # the `pg_url` fixture is provided by conftest.py


@requires_docker
@pytest.mark.asyncio
async def test_postgres_advisory_lock_against_real_postgres(pg_url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.scheduling.adapters.postgres_lock import PostgresAdvisoryLock

    engine = create_async_engine(pg_url)
    try:
        a = PostgresAdvisoryLock(lambda: engine)
        b = PostgresAdvisoryLock(lambda: engine)
        assert await a.try_acquire("job", 30.0) is True
        assert await b.try_acquire("job", 30.0) is False  # advisory lock held on a's connection
        await a.release("job")
        assert await b.try_acquire("job", 30.0) is True  # freed -> b acquires
        await b.release("job")
    finally:
        await engine.dispose()
