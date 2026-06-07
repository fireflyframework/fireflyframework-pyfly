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
"""Unified @transactional (v26.06.75) — ONE annotation dispatching to relational or document.

Proves a single `@transactional` works on both backends and closes the previously-untested
MongoDB transaction path (commit / abort / no_rollback_for-commit / session injection / errors).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyfly.data import Isolation, transactional
from pyfly.data.document.mongodb import mongo_transactional


def test_one_annotation_is_shared_across_backends() -> None:
    from pyfly.data.relational.sqlalchemy import transactional as relational_transactional

    assert transactional is relational_transactional
    assert mongo_transactional is transactional  # deprecated alias


# --------------------------------------------------------------------------- relational dispatch
def _make_session() -> MagicMock:
    session = MagicMock()
    session.begin = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.info = {}
    session.execution_options = MagicMock(return_value=session)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _relational_service(session: MagicMock) -> Any:
    factory = MagicMock(return_value=session)

    class Svc:
        def __init__(self) -> None:
            self._session_factory = factory

        @transactional()
        async def commit_path(self) -> str:
            return "ok"

        @transactional(isolation=Isolation.SERIALIZABLE)
        async def with_isolation(self) -> str:
            return "ok"

        @transactional()
        async def failing(self) -> str:
            raise ValueError("boom")

    return Svc()


@pytest.mark.asyncio
async def test_relational_commits_on_success() -> None:
    session = _make_session()
    await _relational_service(session).commit_path()
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_relational_isolation_execution_option_is_applied() -> None:
    session = _make_session()
    await _relational_service(session).with_isolation()
    # the audit flagged this as unverified: assert the isolation_level actually reaches the session
    session.execution_options.assert_called_once_with(isolation_level="SERIALIZABLE")


@pytest.mark.asyncio
async def test_relational_rolls_back_on_exception() -> None:
    session = _make_session()
    with pytest.raises(ValueError, match="boom"):
        await _relational_service(session).failing()
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


# --------------------------------------------------------------------------- document dispatch
class _FakeTxn:
    """Mimics motor's session.start_transaction() async CM: commit on clean exit, abort on error."""

    def __init__(self, session: _FakeMongoSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._session.committed = True
        else:
            self._session.aborted = True
        return False


class _FakeMongoSession:
    def __init__(self) -> None:
        self.committed = False
        self.aborted = False

    async def __aenter__(self) -> _FakeMongoSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def start_transaction(self) -> _FakeTxn:
        return _FakeTxn(self)


class _FakeMotorClient:
    def __init__(self, session: _FakeMongoSession) -> None:
        self._session = session

    async def start_session(self) -> _FakeMongoSession:
        return self._session


def _document_service(session: _FakeMongoSession) -> Any:
    client = _FakeMotorClient(session)

    class Svc:
        def __init__(self) -> None:
            self._motor_client = client

        @transactional()
        async def commit_path(self, *, session: Any = None) -> Any:
            return session  # session must be injected

        @transactional()
        async def fails(self, *, session: Any = None) -> None:
            raise ValueError("boom")

        @transactional(no_rollback_for=(KeyError,))
        async def fails_no_rollback(self, *, session: Any = None) -> None:
            raise KeyError("ignored")

    return Svc()


@pytest.mark.asyncio
async def test_document_commits_and_injects_session() -> None:
    session = _FakeMongoSession()
    injected = await _document_service(session).commit_path()
    assert injected is session  # session kwarg injected
    assert session.committed and not session.aborted


@pytest.mark.asyncio
async def test_document_aborts_on_rollback_for_exception() -> None:
    session = _FakeMongoSession()
    with pytest.raises(ValueError, match="boom"):
        await _document_service(session).fails()
    assert session.aborted and not session.committed


@pytest.mark.asyncio
async def test_document_commits_on_no_rollback_for_exception() -> None:
    session = _FakeMongoSession()
    with pytest.raises(KeyError):
        await _document_service(session).fails_no_rollback()
    # no_rollback_for -> committed despite the exception, which is still re-raised
    assert session.committed and not session.aborted


@pytest.mark.asyncio
async def test_document_missing_motor_client_raises() -> None:
    class NoClient:
        @transactional()
        async def work(self, *, session: Any = None) -> None: ...

    with pytest.raises(RuntimeError, match="no transaction manager"):
        await NoClient().work()
