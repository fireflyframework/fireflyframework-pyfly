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
"""@pre_filter / @post_filter collection filtering."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyfly.context.request_context import RequestContext
from pyfly.security.context import SecurityContext
from pyfly.security.method_security import post_filter, pre_filter


@pytest.fixture(autouse=True)
def _clear_request_context() -> Any:
    RequestContext.clear()
    yield
    RequestContext.clear()


def _ctx(user: str = "alice", roles: list[str] | None = None) -> None:
    ctx = RequestContext.init()
    ctx.security_context = SecurityContext(user_id=user, roles=roles or [])


def _docs() -> list[SimpleNamespace]:
    return [SimpleNamespace(owner="alice"), SimpleNamespace(owner="bob"), SimpleNamespace(owner="alice")]


@post_filter("filterObject.owner == principal.user_id")
async def list_docs() -> list[SimpleNamespace]:
    return _docs()


@post_filter("filterObject.owner == principal.user_id")
def list_docs_sync() -> list[SimpleNamespace]:
    return _docs()


@pre_filter("filterObject.owner == principal.user_id", filter_target="docs")
async def save_all(docs: list[SimpleNamespace]) -> list[SimpleNamespace]:
    return docs


@pre_filter("filterObject.owner == principal.user_id")
async def save_first_collection(docs: list[SimpleNamespace]) -> list[SimpleNamespace]:
    return docs


class TestPostFilter:
    @pytest.mark.asyncio
    async def test_keeps_only_matching_elements(self) -> None:
        _ctx("alice")
        result = await list_docs()
        assert [d.owner for d in result] == ["alice", "alice"]

    @pytest.mark.asyncio
    async def test_preserves_collection_type(self) -> None:
        _ctx("alice")
        assert isinstance(await list_docs(), list)

    def test_sync_method(self) -> None:
        _ctx("bob")
        assert [d.owner for d in list_docs_sync()] == ["bob"]


class TestPreFilter:
    @pytest.mark.asyncio
    async def test_filters_named_argument(self) -> None:
        _ctx("alice")
        result = await save_all(docs=_docs())
        assert [d.owner for d in result] == ["alice", "alice"]

    @pytest.mark.asyncio
    async def test_filters_positional_argument(self) -> None:
        _ctx("bob")
        result = await save_all(_docs())
        assert [d.owner for d in result] == ["bob"]

    @pytest.mark.asyncio
    async def test_autodetects_first_collection(self) -> None:
        _ctx("alice")
        result = await save_first_collection(_docs())
        assert [d.owner for d in result] == ["alice", "alice"]
