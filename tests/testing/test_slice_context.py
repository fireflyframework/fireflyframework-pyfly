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
"""Functional test slices (v26.06.51): web_slice / service_slice / data_slice."""

from __future__ import annotations

import pytest

from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
from pyfly.container.stereotypes import rest_controller, service
from pyfly.testing import data_slice, service_slice, web_slice
from pyfly.web.mappings import get_mapping, request_mapping


class WidgetService:
    def names(self) -> list[str]:
        return ["real-widget"]


class FakeWidgetService:
    def names(self) -> list[str]:
        return ["fake-widget"]


@rest_controller
@request_mapping("/api/widgets")
class WidgetController:
    def __init__(self, widget_service: WidgetService) -> None:
        self._service = widget_service

    @get_mapping("/")
    async def list_widgets(self) -> dict:
        return {"widgets": self._service.names()}


@service
class GreetingService:
    def __init__(self, widget_service: WidgetService) -> None:
        self._service = widget_service

    def greet(self) -> str:
        return f"hello {self._service.names()[0]}"


@pytest.mark.asyncio
async def test_web_slice_serves_controller_with_real_dependency() -> None:
    async with await web_slice(WidgetController, WidgetService) as (ctx, client):
        assert ctx.get_bean(WidgetController) is not None
        client.get("/api/widgets/").assert_status(200)
        assert client.get("/api/widgets/").json() == {"widgets": ["real-widget"]}


@pytest.mark.asyncio
async def test_web_slice_with_overridden_collaborator_instance() -> None:
    async with await web_slice(WidgetController, overrides={WidgetService: FakeWidgetService()}) as (_ctx, client):
        assert client.get("/api/widgets/").json() == {"widgets": ["fake-widget"]}


@pytest.mark.asyncio
async def test_service_slice_with_override_class() -> None:
    async with await service_slice(GreetingService, overrides={WidgetService: FakeWidgetService}) as ctx:
        assert ctx.get_bean(GreetingService).greet() == "hello fake-widget"


@pytest.mark.asyncio
async def test_data_slice_is_minimal_and_stops_cleanly() -> None:
    async with await data_slice(WidgetService) as ctx:
        assert isinstance(ctx.get_bean(WidgetService), WidgetService)
    # context stopped on exit; a fresh slice is independent
    async with await data_slice(WidgetService) as ctx2:
        assert isinstance(ctx2.get_bean(WidgetService), WidgetService)


@pytest.mark.asyncio
async def test_missing_collaborator_fails_loudly() -> None:
    # WidgetController needs WidgetService, which is neither passed nor overridden.
    with pytest.raises((NoSuchBeanError, BeanCreationException)):
        async with await web_slice(WidgetController):
            pass
