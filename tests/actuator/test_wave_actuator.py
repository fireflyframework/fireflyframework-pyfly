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
"""Regression tests for actuator fixes.

#161 — threaddump className carries the module, not the function name.
#162 — prometheus endpoint degrades gracefully when prometheus_client is absent.
"""

from __future__ import annotations

import pytest

from pyfly.actuator.endpoints import prometheus_endpoint as prom_mod
from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint
from pyfly.actuator.endpoints.threaddump_endpoint import ThreadDumpEndpoint


class TestThreadDumpClassName:
    @pytest.mark.asyncio
    async def test_classname_is_module_not_function(self):
        result = await ThreadDumpEndpoint().handle()
        entries = [e for thread in result["threads"] for e in thread["stackTrace"]]
        assert entries
        # The old bug set className == methodName (both = function name). The
        # module-based className differs from the method name (#161).
        assert any(e["className"] != e["methodName"] for e in entries)
        # className should be a dotted module path for at least one frame.
        assert any("." in e["className"] for e in entries)


class TestPrometheusEndpointGuard:
    @pytest.mark.asyncio
    async def test_enabled_and_serves_when_client_present(self):
        endpoint = PrometheusEndpoint()
        assert endpoint.enabled is True
        result = await endpoint.handle()
        assert "version=0.0.4" in result["content_type"]

    @pytest.mark.asyncio
    async def test_absent_client_degrades_without_raising(self, monkeypatch):
        monkeypatch.setattr(prom_mod, "generate_latest", None)
        endpoint = PrometheusEndpoint()
        assert endpoint.enabled is False
        result = await endpoint.handle()  # must not raise
        assert result["status"] == 503
        assert "not installed" in result["body"]
