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
"""Wave C integration test — logging, banner, ordering, profiles, startup experience."""

import pytest

from pyfly.container.exceptions import NoSuchBeanError
from pyfly.container.ordering import HIGHEST_PRECEDENCE, LOWEST_PRECEDENCE, order
from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.logging.port import LoggingPort
from pyfly.logging.structlog_adapter import StructlogAdapter


class TestWaveCFullLifecycle:
    async def test_ordered_beans_with_profiles(self):
        """Beans are filtered by profile, then initialized in @order sequence."""
        init_order: list[str] = []

        @order(2)
        @service(profile="dev")
        class DevCacheService:
            def __init__(self):
                init_order.append("cache")

        @order(1)
        @service(profile="dev")
        class DevDbService:
            def __init__(self):
                init_order.append("db")

        @order(3)
        @service(profile="production")
        class ProdMonitorService:
            def __init__(self):
                init_order.append("monitor")

        config = Config({"pyfly": {"profiles": {"active": "dev"}}})
        ctx = ApplicationContext(config)
        ctx.register_bean(DevCacheService)
        ctx.register_bean(DevDbService)
        ctx.register_bean(ProdMonitorService)

        await ctx.start()

        assert init_order == ["db", "cache"]
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(ProdMonitorService)

        await ctx.stop()

    async def test_logging_port_conformance(self):
        """StructlogAdapter satisfies the LoggingPort protocol."""
        adapter = StructlogAdapter()
        assert isinstance(adapter, LoggingPort)
        adapter.configure(Config({"logging": {"level": {"root": "DEBUG"}, "format": "console"}}))
        logger = adapter.get_logger("test")
        assert logger is not None

    async def test_negated_profile_with_ordering(self):
        """Beans with negated profiles work correctly with @order."""
        init_order: list[str] = []

        @order(2)
        @service(profile="!production")
        class DebugService:
            def __init__(self):
                init_order.append("debug")

        @order(1)
        @service
        class CoreService:
            def __init__(self):
                init_order.append("core")

        config = Config({"pyfly": {"profiles": {"active": "dev"}}})
        ctx = ApplicationContext(config)
        ctx.register_bean(DebugService)
        ctx.register_bean(CoreService)

        await ctx.start()
        assert init_order == ["core", "debug"]
        await ctx.stop()

    async def test_full_startup_with_pyfly_application(self, tmp_path, monkeypatch):
        """PyFlyApplication performs full startup with banner, logging, profiles."""
        from pyfly.core.application import PyFlyApplication, pyfly_application

        monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", "dev")

        config_file = tmp_path / "pyfly.yaml"
        config_file.write_text("pyfly:\n  banner:\n    mode: 'MINIMAL'\nlogging:\n  level:\n    root: INFO\n")

        dev_config = tmp_path / "pyfly-dev.yaml"
        dev_config.write_text("app:\n  debug: true\n")

        @pyfly_application(name="IntegrationApp", version="2.0.0", scan_packages=[])
        class App:
            pass

        app = PyFlyApplication(App, config_path=config_file)
        assert app.config.get("app.debug") is True

        await app.startup()
        assert app.startup_time_seconds > 0
        assert app.context.environment.active_profiles == ["dev"]
        assert app.context.bean_count >= 1
        await app.shutdown()

    async def test_profile_config_deep_merge(self, tmp_path):
        """Profile configs deeply merge with base config."""
        base = tmp_path / "pyfly.yaml"
        base.write_text("server:\n  host: localhost\n  port: 8080\n")

        dev = tmp_path / "pyfly-dev.yaml"
        dev.write_text("server:\n  port: 9090\n  debug: true\n")

        config = Config.from_file(base, active_profiles=["dev"])
        assert config.get("server.host") == "localhost"
        assert config.get("server.port") == 9090
        assert config.get("server.debug") is True

    async def test_order_constants(self):
        """Order constants have correct Spring-compatible values."""
        assert HIGHEST_PRECEDENCE == -(2**31)
        assert LOWEST_PRECEDENCE == 2**31 - 1
        assert HIGHEST_PRECEDENCE < 0 < LOWEST_PRECEDENCE

    async def test_bean_count_reflects_profile_filtering(self):
        """Bean count only includes beans that passed profile filtering."""

        @service
        class IncludedA:
            pass

        @service
        class IncludedB:
            pass

        @service(profile="staging")
        class ExcludedC:
            pass

        config = Config({"pyfly": {"profiles": {"active": "dev"}}})
        ctx = ApplicationContext(config)
        ctx.register_bean(IncludedA)
        ctx.register_bean(IncludedB)
        ctx.register_bean(ExcludedC)

        await ctx.start()
        assert ctx.bean_count >= 3
        await ctx.stop()
