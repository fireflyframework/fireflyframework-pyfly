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
"""Tests for the Spring-parity repository protocol hierarchy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pyfly.data.ports.outbound import (
    CrudRepository,
    PagingAndSortingRepository,
    ReactiveSortingRepository,
    RepositoryPort,
)


class _Crud:
    async def save(self, entity: Any) -> Any: ...
    async def save_all(self, entities: Any) -> Any: ...
    async def find_by_id(self, id: Any) -> Any: ...
    async def find_all(self, criteria: Any = None, **filters: Any) -> Any: ...
    async def find_all_by_id(self, ids: Any) -> Any: ...
    async def exists_by_id(self, id: Any) -> Any: ...
    async def count(self) -> Any: ...
    async def delete(self, entity: Any) -> None: ...
    async def delete_by_id(self, id: Any) -> None: ...
    async def delete_all_by_id(self, ids: Any) -> None: ...
    async def delete_all(self, entities: Any = None) -> None: ...


class _Paging(_Crud):
    def stream_all(self, criteria: Any = None, **filters: Any) -> AsyncIterator[Any]:  # type: ignore[empty-body]
        ...


class TestCrudRepository:
    def test_runtime_checkable(self) -> None:
        assert isinstance(_Crud(), CrudRepository)

    def test_repository_port_is_crud_alias(self) -> None:
        assert RepositoryPort is CrudRepository
        assert isinstance(_Crud(), RepositoryPort)

    def test_required_method_names(self) -> None:
        expected = {
            "save",
            "save_all",
            "find_by_id",
            "find_all",
            "find_all_by_id",
            "exists_by_id",
            "count",
            "delete",
            "delete_by_id",
            "delete_all_by_id",
            "delete_all",
        }
        attrs = {n for n in dir(CrudRepository) if not n.startswith("_")}
        assert expected.issubset(attrs), f"Missing: {expected - attrs}"

    def test_incomplete_class_is_not_crud(self) -> None:
        class _Partial:
            async def save(self, entity: Any) -> Any: ...

        assert not isinstance(_Partial(), CrudRepository)


class TestSortingAndPaging:
    def test_sorting_is_runtime_checkable(self) -> None:
        assert isinstance(_Paging(), ReactiveSortingRepository)

    def test_paging_is_runtime_checkable(self) -> None:
        assert isinstance(_Paging(), PagingAndSortingRepository)

    def test_sorting_adds_stream_all(self) -> None:
        assert "stream_all" in dir(ReactiveSortingRepository)

    def test_crud_without_stream_is_not_sorting(self) -> None:
        assert not isinstance(_Crud(), ReactiveSortingRepository)
