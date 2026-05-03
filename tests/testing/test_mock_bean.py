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
"""Tests for mock_bean() descriptor."""

from __future__ import annotations

from typing import Protocol
from unittest.mock import AsyncMock

from pyfly.testing.mock import MockBeanDescriptor, mock_bean


class OrderRepository(Protocol):
    async def save(self, entity: object) -> object: ...
    async def find_by_id(self, id: str) -> object | None: ...


class TestMockBeanDescriptor:
    def test_returns_async_mock(self) -> None:
        class MyTest:
            repo = mock_bean(OrderRepository)

        test = MyTest()
        assert isinstance(test.repo, AsyncMock)

    def test_mock_has_spec(self) -> None:
        class MyTest:
            repo = mock_bean(OrderRepository)

        test = MyTest()
        # AsyncMock with spec allows calling methods from the spec
        assert hasattr(test.repo, "save")
        assert hasattr(test.repo, "find_by_id")

    def test_each_instance_gets_own_mock(self) -> None:
        class MyTest:
            repo = mock_bean(OrderRepository)

        test1 = MyTest()
        test2 = MyTest()
        assert test1.repo is not test2.repo

    def test_same_instance_returns_same_mock(self) -> None:
        class MyTest:
            repo = mock_bean(OrderRepository)

        test = MyTest()
        mock1 = test.repo
        mock2 = test.repo
        assert mock1 is mock2

    def test_class_access_returns_descriptor(self) -> None:
        class MyTest:
            repo = mock_bean(OrderRepository)

        assert isinstance(MyTest.repo, MockBeanDescriptor)

    def test_bean_type_property(self) -> None:
        desc = mock_bean(OrderRepository)
        assert isinstance(desc, MockBeanDescriptor)
        assert desc.bean_type is OrderRepository
