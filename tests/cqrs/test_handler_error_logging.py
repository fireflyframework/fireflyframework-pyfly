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
"""CQRS handlers log expected (4xx) errors cleanly, unexpected (5xx) with traceback."""

from __future__ import annotations

import logging

import pytest

from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.kernel.exceptions import InfrastructureException, ValidationException


class _Cmd(CommandHandler[object, None]):
    async def do_handle(self, command: object) -> None:  # pragma: no cover - unused
        return None


class _Qry(QueryHandler[object, None]):
    async def do_handle(self, query: object) -> None:  # pragma: no cover - unused
        return None


@pytest.mark.asyncio
async def test_command_validation_error_logged_warning_without_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfly.cqrs.command.handler"):
        await _Cmd().on_error(object(), ValidationException("bad"))
    rec = next(r for r in caplog.records if "failed" in r.getMessage())
    assert rec.levelno == logging.WARNING
    assert rec.exc_info is None


@pytest.mark.asyncio
async def test_command_infra_error_logged_error_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfly.cqrs.command.handler"):
        await _Cmd().on_error(object(), InfrastructureException("db down"))
    rec = next(r for r in caplog.records if "failed" in r.getMessage())
    assert rec.levelno == logging.ERROR
    assert rec.exc_info is not None


@pytest.mark.asyncio
async def test_query_validation_error_logged_warning_without_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfly.cqrs.query.handler"):
        await _Qry().on_error(object(), ValidationException("bad"))
    rec = next(r for r in caplog.records if "failed" in r.getMessage())
    assert rec.levelno == logging.WARNING
    assert rec.exc_info is None
