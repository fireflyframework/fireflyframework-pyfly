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
"""An ``Optional`` of a parametrized generic resolves like the generic itself.

The Optional branch of the parameter resolver used to look the inner type up with ``resolve()``,
which only knows plain classes. ``async_sessionmaker[AsyncSession] | None``, ``Provider[X] | None``,
``Repository[U, ID] | None``, ``list[X] | None`` and ``Annotated[X, Qualifier("n")] | None`` were
never registration keys, so the lookup failed and the parameter silently received ``None``: the
dependency was registered, and the bean behaved as if it were not.
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from pyfly.container import Qualifier  # noqa: E402
from pyfly.container.container import Container  # noqa: E402
from pyfly.container.provider import Provider  # noqa: E402

T = TypeVar("T")
ID = TypeVar("ID")


class _User:
    pass


class _Repo(Generic[T, ID]):
    pass


class _UserRepo(_Repo[_User, int]):
    pass


class _Widget:
    pass


class _TakesOptionalFactory:
    def __init__(self, factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self.factory = factory


class _TakesOptionalProvider:
    def __init__(self, widgets: Provider[_Widget] | None = None) -> None:
        self.widgets = widgets


class _TakesOptionalRepository:
    def __init__(self, repo: _Repo[_User, int] | None = None) -> None:
        self.repo = repo


class _TakesOptionalList:
    def __init__(self, widgets: list[_Widget] | None = None) -> None:
        self.widgets = widgets


class _TakesOptionalQualified:
    def __init__(self, widget: Annotated[_Widget, Qualifier("special")] | None = None) -> None:
        self.widget = widget


class _TakesMissingOptionalGeneric:
    def __init__(self, repo: _Repo[_Widget, int] | None = None) -> None:
        self.repo = repo


def test_optional_session_factory_receives_the_registered_factory() -> None:
    container = Container()
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker()
    container.register_instance(async_sessionmaker, factory)
    container.register(_TakesOptionalFactory)

    assert container.resolve(_TakesOptionalFactory).factory is factory


def test_optional_provider_receives_a_provider() -> None:
    container = Container()
    container.register(_Widget)
    container.register(_TakesOptionalProvider)

    provider = container.resolve(_TakesOptionalProvider).widgets
    assert isinstance(provider, Provider)
    assert isinstance(provider.get(), _Widget)


def test_optional_parametrized_repository_receives_the_matching_implementation() -> None:
    container = Container()
    container.register(_UserRepo)
    container.bind(_Repo, _UserRepo)
    container.register(_TakesOptionalRepository)

    assert isinstance(container.resolve(_TakesOptionalRepository).repo, _UserRepo)


def test_optional_list_receives_every_bean() -> None:
    container = Container()
    container.register(_Widget)
    container.register(_TakesOptionalList)

    widgets = container.resolve(_TakesOptionalList).widgets
    assert widgets is not None
    assert [type(widget) for widget in widgets] == [_Widget]


def test_optional_qualified_dependency_receives_the_named_bean() -> None:
    container = Container()
    container.register(_Widget, name="special")
    container.register(_TakesOptionalQualified)

    assert isinstance(container.resolve(_TakesOptionalQualified).widget, _Widget)


def test_optional_generic_without_a_candidate_still_receives_none() -> None:
    container = Container()
    container.register(_UserRepo)
    container.bind(_Repo, _UserRepo)
    container.register(_TakesMissingOptionalGeneric)

    assert container.resolve(_TakesMissingOptionalGeneric).repo is None
