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
"""Session concurrency control between two application contexts on one database (WP10b: C154, C155).

Two contexts are two instances of the application: each builds its own datasource registry, SQL session store
(``pyfly.session.store=postgres``), SQL registry and controller from configuration. max-sessions=1 must hold
across them, and an eviction on one must end the session the other holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from pyfly.context.application_context import ApplicationContext
from pyfly.session.concurrency import SessionConcurrencyController
from pyfly.session.ports.outbound import SessionStore
from tests.support.backend_matrix import RelationalBackend

CONCURRENCY = 20


@contextlib.asynccontextmanager
async def _instances(backend: RelationalBackend, strategy: str) -> AsyncIterator[list[tuple[Any, Any]]]:
    overrides = {
        "pyfly.data.relational.ddl-auto": "create",
        "pyfly.session.enabled": "true",
        "pyfly.session.store": "postgres",
        "pyfly.session.concurrency.enabled": "true",
        "pyfly.session.concurrency.registry": "postgres",
        "pyfly.session.concurrency.max-sessions": "1",
        "pyfly.session.concurrency.strategy": strategy,
    }
    contexts = [ApplicationContext(backend.config(overrides)) for _ in range(2)]
    started: list[ApplicationContext] = []
    try:
        for ctx in contexts:
            await ctx.start()
            started.append(ctx)
        yield [(ctx.get_bean(SessionConcurrencyController), ctx.get_bean(SessionStore)) for ctx in contexts]
    finally:
        for ctx in reversed(started):
            await ctx.stop()


async def _login(instance: tuple[Any, Any], principal: str, session_id: str) -> bool:
    controller, store = instance
    await store.save(session_id, {"user": principal}, ttl=600)
    return bool(await controller.on_login(principal, session_id, time.time()))


async def test_max_sessions_one_admits_one_login_across_two_instances(relational_backend: RelationalBackend) -> None:
    async with _instances(relational_backend, "reject-new") as instances:
        results = await asyncio.gather(
            *(_login(instances[index % 2], "alice", f"s{index}") for index in range(CONCURRENCY))
        )

        assert results.count(True) == 1


async def test_an_eviction_on_one_instance_ends_the_session_on_the_other(relational_backend: RelationalBackend) -> None:
    async with _instances(relational_backend, "evict-oldest") as (first, second):
        assert await _login(first, "bob", "on-first")
        assert await _login(second, "bob", "on-second")

        assert await first[1].get("on-first") is None
        assert await first[1].get("on-second") is not None
