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
"""Regression tests for TCC audit fixes (#56 retry off-by-one, #57 error recording)."""

from __future__ import annotations

import pytest

from pyfly.transactional.tcc.annotations import (
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.engine.argument_resolver import TccArgumentResolver
from pyfly.transactional.tcc.engine.execution_orchestrator import TccExecutionOrchestrator
from pyfly.transactional.tcc.engine.participant_invoker import TccParticipantInvoker
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry


def _engine(registry: TccRegistry) -> TccEngine:
    resolver = TccArgumentResolver()
    invoker = TccParticipantInvoker(resolver)
    orchestrator = TccExecutionOrchestrator(invoker)
    return TccEngine(registry=registry, participant_invoker=invoker, orchestrator=orchestrator)


@pytest.mark.asyncio
async def test_try_retry_is_attempts_plus_one() -> None:
    attempts = {"n": 0}

    @tcc(name="retry-tcc")
    class RetryTcc:
        @tcc_participant(id="p1")
        class P1:
            @try_method(retry=2)  # 2 retries → 3 total attempts
            async def do_try(self) -> str:
                attempts["n"] += 1
                raise RuntimeError("always fails")

            @confirm_method()
            async def do_confirm(self) -> None: ...

            @cancel_method()
            async def do_cancel(self) -> None: ...

    registry = TccRegistry()
    registry.register_from_bean(RetryTcc())
    result = await _engine(registry).execute("retry-tcc")

    assert attempts["n"] == 3  # retry=2 yields 3 attempts (was 2 before the fix)
    assert not result.success


@pytest.mark.asyncio
async def test_class_level_retry_is_used_when_method_has_none() -> None:
    attempts = {"n": 0}

    @tcc(name="class-retry-tcc", retry_enabled=True, max_retries=3)
    class ClassRetryTcc:
        @tcc_participant(id="p1")
        class P1:
            @try_method()  # no method-level retry → falls back to class config
            async def do_try(self) -> str:
                attempts["n"] += 1
                raise RuntimeError("always fails")

            @confirm_method()
            async def do_confirm(self) -> None: ...

            @cancel_method()
            async def do_cancel(self) -> None: ...

    registry = TccRegistry()
    registry.register_from_bean(ClassRetryTcc())
    await _engine(registry).execute("class-retry-tcc")

    assert attempts["n"] == 4  # max_retries=3 → 4 attempts


@pytest.mark.asyncio
async def test_failed_participant_error_is_recorded() -> None:
    @tcc(name="err-tcc")
    class ErrTcc:
        @tcc_participant(id="payment")
        class Payment:
            @try_method()
            async def do_try(self) -> str:
                raise ValueError("insufficient funds")

            @confirm_method()
            async def do_confirm(self) -> None: ...

            @cancel_method()
            async def do_cancel(self) -> None: ...

    registry = TccRegistry()
    registry.register_from_bean(ErrTcc())
    result = await _engine(registry).execute("err-tcc")

    assert not result.success
    failed = result.failed_participants()
    assert "payment" in failed
    assert isinstance(failed["payment"].try_error, ValueError)
