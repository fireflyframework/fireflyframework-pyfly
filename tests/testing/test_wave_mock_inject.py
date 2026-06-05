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
"""Regression tests for #7 — mock_bean descriptors are injected into the context."""

from __future__ import annotations

import pytest

from pyfly.testing.fixtures import PyFlyTestCase
from pyfly.testing.mock import mock_bean


class OrderPort:
    async def find(self, order_id: str) -> str:
        return ""


# Underscore prefix so pytest does not try to collect this as a test class.
class _MockInjectCase(PyFlyTestCase):
    repo = mock_bean(OrderPort)


@pytest.mark.asyncio
async def test_mock_bean_resolves_from_context():
    case = _MockInjectCase()
    await case.setup()
    try:
        resolved = case.context.container.resolve(OrderPort)
        # The container resolves the very AsyncMock the descriptor exposes.
        assert resolved is case.repo
        case.repo.find.return_value = "ORDER-1"
        assert await case.context.container.resolve(OrderPort).find("x") == "ORDER-1"
    finally:
        await case.teardown()


@pytest.mark.asyncio
async def test_no_mock_beans_is_harmless():
    case = PyFlyTestCase()
    await case.setup()  # no mock_bean descriptors declared — must not error
    try:
        assert case.context is not None
    finally:
        await case.teardown()
