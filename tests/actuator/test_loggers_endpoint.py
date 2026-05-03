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
"""Tests for the loggers actuator endpoint."""

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
    def test_get_lists_loggers(self):
        client = _make_loggers_client()
        resp = client.get("/actuator/loggers")
        assert resp.status_code == 200
        data = resp.json()
        assert "loggers" in data
        assert "ROOT" in data["loggers"]
        assert "levels" in data

    def test_get_includes_standard_levels(self):
        client = _make_loggers_client()
        data = client.get("/actuator/loggers").json()
        assert "DEBUG" in data["levels"]
        assert "INFO" in data["levels"]

    def test_post_changes_logger_level(self):
        # Create a test logger with a known level
        test_logger = logging.getLogger("pyfly.test.loggers_endpoint")
        test_logger.setLevel(logging.WARNING)

        client = _make_loggers_client()
        resp = client.post(
            "/actuator/loggers",
            content=json.dumps({"logger": "pyfly.test.loggers_endpoint", "level": "DEBUG"}),
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["configuredLevel"] == "DEBUG"
        assert test_logger.level == logging.DEBUG

    def test_post_invalid_level_returns_400(self):
        client = _make_loggers_client()
        resp = client.post(
            "/actuator/loggers",
            content=json.dumps({"logger": "ROOT", "level": "BANANA"}),
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_handle_returns_root_logger(self):
        ep = LoggersEndpoint()
        data = await ep.handle()
        assert "ROOT" in data["loggers"]
        root_info = data["loggers"]["ROOT"]
        assert "configuredLevel" in root_info
        assert "effectiveLevel" in root_info

    @pytest.mark.asyncio
    async def test_set_logger_level_direct(self):
        ep = LoggersEndpoint()
        result = await ep.set_logger_level("pyfly.test.direct", "ERROR")
        assert result["configuredLevel"] == "ERROR"
