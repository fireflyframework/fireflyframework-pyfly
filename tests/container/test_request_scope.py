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
"""Tests for REQUEST scope resolution in the DI container."""

import pytest

from pyfly.container.container import Container
from pyfly.container.types import Scope
from pyfly.context.request_context import RequestContext


class DummyRequestService:
    """A test service that should be request-scoped."""

    pass


class TestRequestScope:
    """Verify Container resolves REQUEST-scoped beans via RequestContext."""

    def test_request_scope_raises_without_context(self):
        """Resolving a REQUEST-scoped bean outside a request raises."""
        container = Container()
        container.register(DummyRequestService, scope=Scope.REQUEST)
        with pytest.raises(RuntimeError, match="No active request context"):
            container.resolve(DummyRequestService)

    def test_request_scope_returns_same_instance_within_request(self):
        """Within a single request, the same instance is returned."""
        container = Container()
        container.register(DummyRequestService, scope=Scope.REQUEST)

        _ctx = RequestContext.init()
        try:
            a = container.resolve(DummyRequestService)
            b = container.resolve(DummyRequestService)
            assert a is b
        finally:
            RequestContext.clear()

    def test_request_scope_returns_different_instances_across_requests(self):
        """Different requests get different instances."""
        container = Container()
        container.register(DummyRequestService, scope=Scope.REQUEST)

        _ctx1 = RequestContext.init()
        a = container.resolve(DummyRequestService)
        RequestContext.clear()

        _ctx2 = RequestContext.init()
        b = container.resolve(DummyRequestService)
        RequestContext.clear()

        assert a is not b

    def test_request_scope_cleared_after_request(self):
        """After clearing context, resolving fails again."""
        container = Container()
        container.register(DummyRequestService, scope=Scope.REQUEST)

        _ctx = RequestContext.init()
        container.resolve(DummyRequestService)
        RequestContext.clear()

        with pytest.raises(RuntimeError, match="No active request context"):
            container.resolve(DummyRequestService)
