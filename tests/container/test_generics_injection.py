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
"""Generics-aware injection (v26.06.26): ``Repository[User]`` resolves to the impl
parametrized with ``User`` (Spring generic-aware injection)."""

from __future__ import annotations

from typing import Generic, TypeVar

import pytest

from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError

T = TypeVar("T")
ID = TypeVar("ID")


class _User:
    pass


class _Order:
    pass


class _Repo(Generic[T, ID]):
    pass


class _UserRepo(_Repo[_User, int]):
    pass


class _OrderRepo(_Repo[_Order, int]):
    pass


class _UserService:
    def __init__(self, repo: _Repo[_User, int]) -> None:
        self.repo = repo


class _MissingService:
    def __init__(self, repo: _Repo[_Order, int]) -> None:
        self.repo = repo


def _container() -> Container:
    c = Container()
    c.register(_UserRepo)
    c.register(_OrderRepo)
    c.bind(_Repo, _UserRepo)
    c.bind(_Repo, _OrderRepo)
    return c


def test_generic_param_resolves_by_type_argument() -> None:
    c = _container()
    c.register(_UserService)
    svc = c.resolve(_UserService)
    assert isinstance(svc.repo, _UserRepo)  # _Repo[_User, int] -> _UserRepo, not _OrderRepo


def test_generic_direct_resolution() -> None:
    c = _container()
    repo = c._resolve_param(_Repo[_Order, int])
    assert isinstance(repo, _OrderRepo)


def test_generic_no_matching_parametrization_raises() -> None:
    c = Container()
    c.register(_UserRepo)
    c.bind(_Repo, _UserRepo)  # only a _User repo registered
    c.register(_MissingService)
    with pytest.raises(NoSuchBeanError):
        c.resolve(_MissingService)  # asks for _Repo[_Order, int] — no match
