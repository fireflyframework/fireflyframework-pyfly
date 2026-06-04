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
"""Base test case and fixtures for PyFly applications."""

from __future__ import annotations

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.testing.mock import MockBeanDescriptor


class PyFlyTestCase:
    """Base test case providing pre-configured ApplicationContext and event bus.

    Subclass this for integration tests that need framework infrastructure:

        class TestOrderService(PyFlyTestCase):
            repo = mock_bean(OrderRepository)

            async def test_create_order(self):
                await self.setup()
                # self.context.get_bean(OrderRepository) resolves self.repo
                await self.teardown()
    """

    context: ApplicationContext
    event_bus: InMemoryEventBus

    async def setup(self) -> None:
        """Initialize test infrastructure."""
        self.context = ApplicationContext(Config({}))
        self.event_bus = InMemoryEventBus()
        await self.context.start()
        self._install_mock_beans()

    def _install_mock_beans(self) -> None:
        """Register every ``mock_bean(...)`` descriptor's AsyncMock into the
        container, keyed on its bean type, so DI-resolved collaborators receive
        the mock instead of the real/absent bean (audit #7).
        """
        container = self.context.container
        for klass in type(self).__mro__:
            for attr_name, attr in list(vars(klass).items()):
                if isinstance(attr, MockBeanDescriptor):
                    mock_instance = getattr(self, attr_name)  # materialize the per-instance AsyncMock
                    container.register(attr.bean_type)
                    container._registrations[attr.bean_type].instance = mock_instance

    async def teardown(self) -> None:
        """Clean up test infrastructure."""
        await self.context.stop()
