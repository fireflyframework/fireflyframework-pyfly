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
"""mock_bean() descriptor for providing AsyncMock instances in test classes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock


class MockBeanDescriptor:
    """Descriptor that provides an AsyncMock with spec of the given bean type.

    Usage in test classes::

        class OrderServiceTest(PyFlyTestCase):
            repo = mock_bean(OrderRepository)

            async def test_create(self):
                self.repo.save.return_value = Order(id="1")
                # ... self.repo is an AsyncMock(spec=OrderRepository)
    """

    def __init__(self, bean_type: type) -> None:
        self._bean_type = bean_type
        self._attr_name: str = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr_name = f"_mock_{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        if not hasattr(obj, self._attr_name):
            mock = AsyncMock(spec=self._bean_type)
            setattr(obj, self._attr_name, mock)
        return getattr(obj, self._attr_name)

    @property
    def bean_type(self) -> type:
        """The type this mock replaces."""
        return self._bean_type


def mock_bean(bean_type: type) -> Any:
    """Create a mock bean descriptor for use in test classes.

    The descriptor lazily creates an ``AsyncMock(spec=bean_type)``
    per test instance, so each test gets a fresh mock.

    Args:
        bean_type: The type to mock (used as AsyncMock spec).

    Returns:
        A descriptor that provides an AsyncMock instance.
    """
    return MockBeanDescriptor(bean_type)
