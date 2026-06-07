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
"""@transactional fixes from the final parity audit (v26.06.34):

1. asyncio.CancelledError (a BaseException, not Exception) must ROLL BACK, never
   commit partial work — previously the else-branch committed on cancellation.
2. no_rollback_for commits on matching exceptions.
3. read_only=True enters the routing read_only() scope and flags the session.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyfly.data.relational.routing import is_read_only
from pyfly.data.relational.sqlalchemy.transactional import transactional


def _make_session_factory() -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    session.begin = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execution_options = MagicMock(return_value=session)
    session.info = {}

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = cm
    return factory, session


class _Svc:
    def __init__(self, factory: MagicMock) -> None:
        self._session_factory = factory

    @transactional()
    async def cancel(self) -> None:
        raise asyncio.CancelledError

    @transactional(rollback_for=(ValueError,), no_rollback_for=(KeyError,))
    async def raise_keyerror(self) -> None:
        raise KeyError("k")

    @transactional(read_only=True)
    async def read(self, sink: list[bool]) -> str:
        sink.append(is_read_only())
        return "r"


@pytest.mark.asyncio
async def test_cancellederror_rolls_back_not_commits() -> None:
    factory, session = _make_session_factory()
    with pytest.raises(asyncio.CancelledError):
        await _Svc(factory).cancel()
    session.rollback.assert_awaited()
    session.commit.assert_not_awaited()  # the silent-failure fix: never commit on cancellation


@pytest.mark.asyncio
async def test_no_rollback_for_commits() -> None:
    factory, session = _make_session_factory()
    with pytest.raises(KeyError):
        await _Svc(factory).raise_keyerror()
    session.commit.assert_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_enters_routing_scope_and_flags_session() -> None:
    factory, session = _make_session_factory()
    sink: list[bool] = []
    assert is_read_only() is False
    await _Svc(factory).read(sink)
    assert sink == [True]  # read_only() routing scope active inside the transaction
    assert is_read_only() is False  # restored afterward
    assert session.info.get("read_only") is True
