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
"""SQL-backed UserDetailsService."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from pyfly.security.adapters.sql_user_details import SqlUserDetailsService  # noqa: E402
from pyfly.security.user_details import UserDetails, UserDetailsService  # noqa: E402


@pytest.fixture
def engine() -> Any:
    return create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _svc(engine: Any) -> SqlUserDetailsService:
    return SqlUserDetailsService(lambda: engine)


class TestSqlUserDetailsService:
    @pytest.mark.asyncio
    async def test_save_and_load(self, engine: Any) -> None:
        svc = _svc(engine)
        await svc.save(UserDetails(username="alice", password_hash="h", roles=["ADMIN"], permissions=["read"]))
        user = await svc.load_user_by_username("alice")
        assert user is not None
        assert user.username == "alice"
        assert user.password_hash == "h"
        assert user.roles == ["ADMIN"]
        assert user.permissions == ["read"]
        assert user.enabled is True

    @pytest.mark.asyncio
    async def test_unknown_user_is_none(self, engine: Any) -> None:
        assert await _svc(engine).load_user_by_username("ghost") is None

    @pytest.mark.asyncio
    async def test_save_upserts(self, engine: Any) -> None:
        svc = _svc(engine)
        await svc.save(UserDetails(username="a", password_hash="h1"))
        await svc.save(UserDetails(username="a", password_hash="h2", roles=["X"]))
        user = await svc.load_user_by_username("a")
        assert user is not None and user.password_hash == "h2" and user.roles == ["X"]

    @pytest.mark.asyncio
    async def test_disabled_roundtrips(self, engine: Any) -> None:
        svc = _svc(engine)
        await svc.save(UserDetails(username="d", password_hash="h", enabled=False))
        user = await svc.load_user_by_username("d")
        assert user is not None and user.enabled is False

    @pytest.mark.asyncio
    async def test_delete(self, engine: Any) -> None:
        svc = _svc(engine)
        await svc.save(UserDetails(username="gone", password_hash="h"))
        await svc.delete("gone")
        assert await svc.load_user_by_username("gone") is None

    def test_protocol_conformance(self, engine: Any) -> None:
        assert isinstance(_svc(engine), UserDetailsService)

    def test_invalid_table_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="table"):
            SqlUserDetailsService(lambda: object(), table="users; DROP TABLE x")
