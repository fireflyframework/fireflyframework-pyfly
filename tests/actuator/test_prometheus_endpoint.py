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
"""Tests for PrometheusEndpoint — Prometheus text exposition."""

from __future__ import annotations

import pytest

from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint


class TestPrometheusEndpoint:
    def test_endpoint_id(self) -> None:
        ep = PrometheusEndpoint()
        assert ep.endpoint_id == "prometheus"

    def test_enabled_by_default(self) -> None:
        ep = PrometheusEndpoint()
        assert ep.enabled is True

    @pytest.mark.asyncio
    async def test_handle_returns_text_body(self) -> None:
        ep = PrometheusEndpoint()
        data = await ep.handle()

        assert "body" in data
        assert isinstance(data["body"], str)
        assert "content_type" in data
