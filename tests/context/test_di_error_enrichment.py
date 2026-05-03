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
"""Tests for ApplicationContext error enrichment (Task 4)."""

import pytest

from pyfly.container.bean import bean
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
from pyfly.container.stereotypes import configuration, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.lifecycle import post_construct
from pyfly.core.config import Config


class MissingDep:
    pass


class TestBeanMethodErrors:
    @pytest.mark.asyncio
    async def test_bean_method_error_includes_config_and_method(self):
        """@bean method errors include configuration class and method name."""

        @configuration
        class BadConfig:
            @bean
            def produce_something(self, dep: MissingDep) -> str:
                return "never reached"

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(BadConfig)

        with pytest.raises(NoSuchBeanError) as exc_info:
            await ctx.start()

        err = exc_info.value
        assert "BadConfig" in (err.required_by or "")
        assert "produce_something" in (err.required_by or "")


class TestPostConstructErrors:
    @pytest.mark.asyncio
    async def test_post_construct_failure_wraps_with_context(self):
        """@post_construct failures include bean class and method name."""

        @service
        class FailingService:
            @post_construct
            def setup(self):
                raise RuntimeError("init boom")

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(FailingService)

        with pytest.raises(BeanCreationException) as exc_info:
            await ctx.start()

        err = exc_info.value
        assert err.subsystem == "lifecycle"
        assert "FailingService" in err.provider
        assert "setup" in err.reason
        assert "init boom" in err.reason


class TestPreDestroyErrors:
    @pytest.mark.asyncio
    async def test_pre_destroy_failure_does_not_crash(self, caplog):
        """@pre_destroy failures are logged but do not abort shutdown."""
        from pyfly.context.lifecycle import pre_destroy

        @service
        class FragileService:
            @pre_destroy
            def teardown(self):
                raise RuntimeError("cleanup boom")

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(FragileService)
        await ctx.start()

        # Should not raise
        await ctx.stop()


class TestStartupCatchAll:
    @pytest.mark.asyncio
    async def test_unknown_error_uses_exception_type_as_provider(self):
        """The start() catch-all uses the exception type name instead of 'unknown'."""
        ctx = ApplicationContext(Config({}))

        # Monkey-patch _do_start to raise an arbitrary error
        async def _boom() -> None:
            raise ValueError("something unexpected")

        ctx._do_start = _boom

        with pytest.raises(BeanCreationException) as exc_info:
            await ctx.start()

        err = exc_info.value
        assert err.subsystem == "startup"
        assert "ValueError" in err.provider
        assert "unknown" not in err.provider
