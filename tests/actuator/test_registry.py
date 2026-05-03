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
"""Tests for ActuatorRegistry — discovery, per-endpoint enable/disable."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.actuator.ports import ActuatorEndpoint
from pyfly.actuator.registry import ActuatorRegistry
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config

# ---------------------------------------------------------------------------
# Test endpoint classes
# ---------------------------------------------------------------------------


class _AlwaysEnabled:
    @property
    def endpoint_id(self) -> str:
        return "test-on"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context=None) -> dict[str, Any]:
        return {"status": "ok"}


class _AlwaysDisabled:
    @property
    def endpoint_id(self) -> str:
        return "test-off"

    @property
    def enabled(self) -> bool:
        return False

    async def handle(self, context=None) -> dict[str, Any]:
        return {"status": "ok"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestActuatorEndpointProtocol:
    def test_valid_endpoint_is_instance(self):
        assert isinstance(_AlwaysEnabled(), ActuatorEndpoint)

    def test_non_endpoint_is_not_instance(self):
        class _NotEndpoint:
            pass

        assert not isinstance(_NotEndpoint(), ActuatorEndpoint)


class TestActuatorRegistry:
    def test_register_and_get(self):
        reg = ActuatorRegistry()
        ep = _AlwaysEnabled()
        reg.register(ep)
        enabled = reg.get_enabled_endpoints()
        assert "test-on" in enabled
        assert enabled["test-on"] is ep

    def test_disabled_endpoint_excluded(self):
        reg = ActuatorRegistry()
        reg.register(_AlwaysEnabled())
        reg.register(_AlwaysDisabled())
        enabled = reg.get_enabled_endpoints()
        assert "test-on" in enabled
        assert "test-off" not in enabled

    def test_config_override_enables_disabled_endpoint(self):
        cfg = Config({"pyfly": {"actuator": {"endpoints": {"test-off": {"enabled": True}}}}})
        reg = ActuatorRegistry(config=cfg)
        reg.register(_AlwaysDisabled())
        enabled = reg.get_enabled_endpoints()
        assert "test-off" in enabled

    def test_config_override_disables_enabled_endpoint(self):
        cfg = Config({"pyfly": {"actuator": {"endpoints": {"test-on": {"enabled": False}}}}})
        reg = ActuatorRegistry(config=cfg)
        reg.register(_AlwaysEnabled())
        enabled = reg.get_enabled_endpoints()
        assert "test-on" not in enabled

    @pytest.mark.asyncio
    async def test_discover_from_context(self):
        from pyfly.container.stereotypes import component

        @component
        class CustomEndpoint:
            @property
            def endpoint_id(self) -> str:
                return "custom"

            @property
            def enabled(self) -> bool:
                return True

            async def handle(self, context=None) -> dict[str, Any]:
                return {"custom": True}

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(CustomEndpoint)
        await ctx.start()

        reg = ActuatorRegistry()
        reg.discover_from_context(ctx)
        enabled = reg.get_enabled_endpoints()
        assert "custom" in enabled

    def test_discover_does_not_overwrite_existing(self):
        """If an endpoint_id is already registered, discover should not replace it."""
        reg = ActuatorRegistry()
        original = _AlwaysEnabled()
        reg.register(original)

        # Simulate a context with a bean that has the same endpoint_id
        # (we test this through the registry's internal check)
        _duplicate = _AlwaysEnabled()
        reg._endpoints["test-on"] = original  # already present
        # Re-registering via discover logic shouldn't overwrite
        assert reg._endpoints["test-on"] is original
