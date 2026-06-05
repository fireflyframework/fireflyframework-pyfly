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
"""Regression tests for orchestration REST fixes.

#167 — DeadLetter GET /count endpoint + DeadLetterService.count().
#169 — GET /executions defaults to in-flight executions, not the whole store.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.transactional.core.dlq import DeadLetterService
from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
from pyfly.transactional.core.persistence import ExecutionPersistenceProvider, ExecutionState
from pyfly.web.adapters.starlette.app import create_app


def _ctx() -> ApplicationContext:
    return ApplicationContext(Config({"pyfly": {"transactional": {"enabled": "true"}}}))


def _state(cid: str, status: ExecutionStatus) -> ExecutionState:
    now = datetime.now(UTC)
    return ExecutionState(
        correlation_id=cid,
        name="demo",
        pattern=ExecutionPattern.SAGA,
        status=status,
        started_at=now,
        updated_at=now,
        completed_at=None,
        payload={},
    )


class TestListExecutionsDefaultsToInFlight:
    @pytest.mark.asyncio
    async def test_default_returns_in_flight_only(self):
        ctx = _ctx()
        await ctx.start()
        try:
            persistence = ctx.get_bean(ExecutionPersistenceProvider)
            await persistence.save(_state("run-active", ExecutionStatus.RUNNING))
            await persistence.save(_state("run-done", ExecutionStatus.COMPLETED))
            client = TestClient(create_app(context=ctx))
            body = client.get("/api/orchestration/executions").json()
            assert {e["correlation_id"] for e in body} == {"run-active"}  # #169
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_explicit_status_filter_still_works(self):
        ctx = _ctx()
        await ctx.start()
        try:
            persistence = ctx.get_bean(ExecutionPersistenceProvider)
            await persistence.save(_state("run-active", ExecutionStatus.RUNNING))
            await persistence.save(_state("run-done", ExecutionStatus.COMPLETED))
            client = TestClient(create_app(context=ctx))
            body = client.get("/api/orchestration/executions?status=COMPLETED").json()
            assert {e["correlation_id"] for e in body} == {"run-done"}
        finally:
            await ctx.stop()


class TestDeadLetterCount:
    @pytest.mark.asyncio
    async def test_count_endpoint_reflects_captures(self):
        ctx = _ctx()
        await ctx.start()
        try:
            dlq = ctx.get_bean(DeadLetterService)
            client = TestClient(create_app(context=ctx))
            assert client.get("/api/orchestration/dlq/count").json() == {"count": 0}
            await dlq.capture(execution_name="x", correlation_id="c1", error=ValueError("boom"))
            await dlq.capture(execution_name="y", correlation_id="c2", error=ValueError("boom2"))
            assert client.get("/api/orchestration/dlq/count").json() == {"count": 2}
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_service_count(self):
        svc = DeadLetterService()
        assert await svc.count() == 0
        await svc.capture(execution_name="x", correlation_id="c", error=RuntimeError("e"))
        assert await svc.count() == 1
