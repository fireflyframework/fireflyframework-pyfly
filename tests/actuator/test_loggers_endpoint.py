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
"""Tests for the loggers actuator endpoint — Spring Boot parity."""

from __future__ import annotations

import json
import logging

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
from pyfly.actuator.endpoints import LoggersEndpoint
from pyfly.actuator.registry import ActuatorRegistry


def _make_loggers_client() -> TestClient:
    registry = ActuatorRegistry()
    registry.register(LoggersEndpoint())
    routes = make_starlette_actuator_routes(registry)
    app = Starlette(routes=routes)
    return TestClient(app)


class TestLoggersEndpoint:
    def test_get_lists_loggers_levels_and_groups(self):
        client = _make_loggers_client()
        resp = client.get("/actuator/loggers")
        assert resp.status_code == 200
        data = resp.json()
        assert "ROOT" in data["loggers"]
        # Spring level vocabulary, not Python's.
        assert data["levels"] == ["OFF", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"]
        assert "groups" in data

    def test_levels_use_spring_names_not_python(self):
        test_logger = logging.getLogger("pyfly.test.levelnames")
        test_logger.setLevel(logging.WARNING)
        client = _make_loggers_client()
        data = client.get("/actuator/loggers").json()
        # WARNING -> WARN (Spring), never the Python name.
        assert data["loggers"]["pyfly.test.levelnames"]["configuredLevel"] == "WARN"

    def test_get_single_logger_by_name(self):
        test_logger = logging.getLogger("pyfly.test.single")
        test_logger.setLevel(logging.DEBUG)
        client = _make_loggers_client()
        resp = client.get("/actuator/loggers/pyfly.test.single")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configuredLevel"] == "DEBUG"
        assert "effectiveLevel" in body

    def test_post_sets_level_via_path_and_returns_204(self):
        test_logger = logging.getLogger("pyfly.test.postset")
        test_logger.setLevel(logging.WARNING)
        client = _make_loggers_client()
        resp = client.post(
            "/actuator/loggers/pyfly.test.postset",
            content=json.dumps({"configuredLevel": "DEBUG"}),
        )
        assert resp.status_code == 204
        assert test_logger.level == logging.DEBUG

    def test_post_null_resets_level(self):
        test_logger = logging.getLogger("pyfly.test.reset")
        test_logger.setLevel(logging.DEBUG)
        client = _make_loggers_client()
        resp = client.post(
            "/actuator/loggers/pyfly.test.reset",
            content=json.dumps({"configuredLevel": None}),
        )
        assert resp.status_code == 204
        assert test_logger.level == logging.NOTSET

    def test_post_accepts_trace_and_off(self):
        client = _make_loggers_client()
        for level in ("TRACE", "OFF"):
            resp = client.post(
                "/actuator/loggers/pyfly.test.traceoff",
                content=json.dumps({"configuredLevel": level}),
            )
            assert resp.status_code == 204

    def test_post_invalid_level_returns_400(self):
        client = _make_loggers_client()
        resp = client.post(
            "/actuator/loggers/ROOT",
            content=json.dumps({"configuredLevel": "BANANA"}),
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_handle_returns_root_logger(self):
        ep = LoggersEndpoint()
        data = await ep.handle()
        assert "ROOT" in data["loggers"]
        assert "configuredLevel" in data["loggers"]["ROOT"]
        assert "effectiveLevel" in data["loggers"]["ROOT"]

    @pytest.mark.asyncio
    async def test_set_logger_level_direct_returns_none_on_success(self):
        ep = LoggersEndpoint()
        result = await ep.set_logger_level("pyfly.test.direct", "ERROR")
        assert result is None
        assert logging.getLogger("pyfly.test.direct").level == logging.ERROR
