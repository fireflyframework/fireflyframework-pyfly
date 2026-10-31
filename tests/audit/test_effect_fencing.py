# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

import asyncio
import subprocess
import sys
from contextvars import ContextVar

import pytest

from pyfly.audit._effects import admitted_audit_effect, has_admitted_audit_effect
from pyfly.resilience.retry import retry


def test_nested_admission_fence_resets_and_cannot_be_set_by_lookalike_context():
    fake = ContextVar("pyfly.audit.admitted_effects", default=())
    fake.set((object(),))
    assert not has_admitted_audit_effect()
    with admitted_audit_effect(object()):
        assert has_admitted_audit_effect()
        with pytest.raises(ValueError), admitted_audit_effect(object()):
            raise ValueError("fixture")
        assert has_admitted_audit_effect()
    assert not has_admitted_audit_effect()


async def test_fence_follows_child_context_without_leaking_parallel_tasks():
    entered, release = asyncio.Event(), asyncio.Event()

    async def active():
        with admitted_audit_effect(object()):
            entered.set()
            await release.wait()

            async def child():
                return has_admitted_audit_effect()

            assert await asyncio.create_task(child())
            with pytest.raises(asyncio.CancelledError), admitted_audit_effect(object()):
                raise asyncio.CancelledError()
        assert not has_admitted_audit_effect()

    task = asyncio.create_task(active())
    await entered.wait()
    assert not has_admitted_audit_effect()
    release.set()
    await task


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_generic_retry_has_one_attempt_inside_admission_and_preserves_terminal_property(asynchronous):
    class Terminal(Exception):
        @property
        def retryable(self):
            return False

    calls = []

    def fail():
        calls.append(True)
        raise ValueError("fixture")

    async def async_fail():
        fail()

    wrapped = retry(max_attempts=3)(async_fail if asynchronous else fail)
    with admitted_audit_effect(object()), pytest.raises(ValueError):
        if asynchronous:
            await wrapped()
        else:
            wrapped()
    assert len(calls) == 1
    with pytest.raises(ValueError):
        if asynchronous:
            await wrapped()
        else:
            wrapped()
    assert len(calls) == 4

    @retry(max_attempts=3)
    def terminal():
        calls.append(True)
        raise Terminal()

    with pytest.raises(Terminal):
        terminal()
    assert len(calls) == 5


def test_generic_retry_fence_requires_no_sql_ai_or_crypto_imports():
    script = """
import sys
from pyfly.resilience.retry import retry
@retry(max_attempts=1)
def fail():
    raise ValueError()
try:
    fail()
except ValueError:
    pass
assert not any(name.split('.')[0] in {'sqlalchemy', 'pydantic_ai', 'cryptography'} for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
