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


# ---------------------------------------------------------------------------------------------------------
# Data slices on a real database: fail fast, stop what failed, roll back (C138, C156, C176)
# ---------------------------------------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from sqlalchemy import Integer, String  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from pyfly.container.stereotypes import repository  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceRegistry  # noqa: E402
from pyfly.data.relational.sqlalchemy import Base, Repository  # noqa: E402


class _SliceMember(Base):
    __tablename__ = "wp11_slice_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(80))


@repository
class SliceMemberRepository(Repository[_SliceMember, int]):
    async def find_by_email(self, email: str) -> _SliceMember | None: ...

    async def exists_by_email(self, email: str) -> bool: ...


class _Missing:
    pass


@service
class _NeedsMissing:
    def __init__(self, missing: _Missing) -> None:
        self._missing = missing


def _relational(url: str, **extra: object) -> Config:
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, **extra}}}})


@pytest.mark.asyncio
async def test_a_slice_that_fails_fast_is_stopped(tmp_path: Path) -> None:
    """C176: the started context of a slice whose check fails is stopped, so its pools are closed."""
    config = _relational(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    registry = DataSourceRegistry.for_config(config)
    with pytest.raises((NoSuchBeanError, BeanCreationException)):
        await data_slice(SliceMemberRepository, _NeedsMissing, config=config)
    assert registry.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("relational", [{}, {"url": "sqlite+aiosqlite:///{db}"}])
async def test_a_repository_without_the_relational_layer_fails_the_slice(
    tmp_path: Path, relational: dict[str, str]
) -> None:
    """C138: without pyfly.data.relational.enabled the derived queries are never compiled, and their stubs
    answered None with no database at all; the slice refuses to build instead, naming the flag."""
    tree = {key: value.format(db=tmp_path / "app.db") for key, value in relational.items()}
    config = Config({"pyfly": {"data": {"relational": tree}}})
    with pytest.raises(BeanCreationException, match=r"pyfly\.data\.relational\.enabled"):
        await data_slice(SliceMemberRepository, config=config)


@pytest.mark.asyncio
async def test_a_data_slice_rolls_back_the_test_on_an_in_memory_database() -> None:
    """The single connection of an in-memory SQLite database is the test's connection."""
    config = _relational("sqlite+aiosqlite:///:memory:")
    async with await data_slice(SliceMemberRepository, config=config, rollback=True) as ctx:
        members = ctx.get_bean(SliceMemberRepository)
        await members.save(_SliceMember(email="in@memory"))
        assert (await members.find_by_email("in@memory")) is not None
        assert await members.exists_by_email("in@memory") is True


@pytest.mark.asyncio
async def test_a_file_database_keeps_nothing_a_rolled_back_slice_wrote(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    for _test in range(2):
        async with await data_slice(SliceMemberRepository, config=_relational(url), rollback=True) as ctx:
            members = ctx.get_bean(SliceMemberRepository)
            assert await members.count() == 0
            await members.save(_SliceMember(email="once@example.com"))
