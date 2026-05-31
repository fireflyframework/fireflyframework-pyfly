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
"""Integration tests: transactional REST controllers are mounted as HTTP routes.

Regression for the bug where ``OrchestrationController`` / ``DeadLetterController`` /
``WorkflowController`` were registered as beans but never mounted because the
classes lacked web stereotype/mapping decorators, so the ``ControllerRegistrar``
skipped them.
"""

from __future__ import annotations

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


def _transactional_context() -> ApplicationContext:
    """Build a context with the full transactional stack enabled."""
    return ApplicationContext(Config({"pyfly": {"transactional": {"enabled": "true"}}}))


class TestTransactionalControllerMounting:
    """The three REST controllers are discovered and mounted by create_app()."""

    @pytest.mark.asyncio
    async def test_routes_are_mounted(self) -> None:
        ctx = _transactional_context()
        await ctx.start()

        app = create_app(context=ctx)

        paths = {route.path for route in app.routes if isinstance(route, Route)}
        assert "/api/orchestration/executions" in paths
        assert "/api/orchestration/executions/{correlation_id}" in paths
        assert "/api/orchestration/dlq" in paths
        assert "/api/orchestration/dlq/{entry_id}" in paths
        assert "/api/orchestration/dlq/{entry_id}/retry" in paths
        assert "/api/orchestration/workflow/start" in paths
        assert "/api/orchestration/workflow/signal" in paths

    @pytest.mark.asyncio
    async def test_list_executions_returns_200(self) -> None:
        ctx = _transactional_context()
        await ctx.start()

        app = create_app(context=ctx)
        client = TestClient(app)

        response = client.get("/api/orchestration/executions")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_dlq_returns_200(self) -> None:
        ctx = _transactional_context()
        await ctx.start()

        app = create_app(context=ctx)
        client = TestClient(app)

        response = client.get("/api/orchestration/dlq")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_get_unknown_execution_returns_no_content(self) -> None:
        ctx = _transactional_context()
        await ctx.start()

        app = create_app(context=ctx)
        client = TestClient(app)

        # The handler returns ``None`` for an unknown id; the framework maps a
        # ``None`` return value to ``204 No Content``.
        response = client.get("/api/orchestration/executions/does-not-exist")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_dlq_delete_unknown_returns_200(self) -> None:
        ctx = _transactional_context()
        await ctx.start()

        app = create_app(context=ctx)
        client = TestClient(app)

        response = client.delete("/api/orchestration/dlq/missing")
        assert response.status_code == 200
        assert response.json() == {"deleted": False}

    @pytest.mark.asyncio
    async def test_workflow_signal_binds_json_body(self) -> None:
        ctx = _transactional_context()
        await ctx.start()

        app = create_app(context=ctx)
        client = TestClient(app)

        # Body[SignalRequest] is validated and bound; an unknown correlation id
        # is simply not delivered (no error raised).
        response = client.post(
            "/api/orchestration/workflow/signal",
            json={"correlation_id": "unknown", "signal": "approve", "payload": {"ok": True}},
        )
        assert response.status_code == 200
        assert response.json() == {"delivered": False}
