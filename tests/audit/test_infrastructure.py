# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Internal recursion trust follows execution context, never application event data."""

import asyncio
import importlib.util
from contextvars import ContextVar

import pytest


def marker():
    assert importlib.util.find_spec("pyfly.audit._infrastructure") is not None
    from pyfly.audit._infrastructure import audit_infrastructure, is_audit_infrastructure

    return audit_infrastructure, is_audit_infrastructure


def test_infrastructure_marker_is_nested_and_restores_after_failure():
    guard, active = marker()
    assert not active()
    with guard():
        assert active()
        with pytest.raises(RuntimeError), guard():
            assert active()
            raise RuntimeError("fixture failure")
        assert active()
    assert not active()


async def test_infrastructure_marker_propagates_to_children_without_leaking_parallel_contexts():
    guard, active = marker()
    entered, release = asyncio.Event(), asyncio.Event()

    async def protected():
        with guard():
            entered.set()
            await release.wait()

            async def child():
                return active()

            assert await asyncio.create_task(child())

    task = asyncio.create_task(protected())
    await entered.wait()
    assert not active()
    release.set()
    await task
    assert not active()


def test_external_lookalike_context_and_event_names_cannot_enable_marker():
    _, active = marker()
    fake = ContextVar("pyfly.audit.infrastructure", default=False)
    fake.set(True)
    assert not active()


async def test_marker_is_reset_after_cancelled_scope():
    guard, active = marker()
    with pytest.raises(asyncio.CancelledError), guard():
        assert active()
        raise asyncio.CancelledError()
    assert not active()


async def test_async_infrastructure_decorator_stays_active_until_await_and_preserves_signature():
    import inspect

    from pyfly.audit._infrastructure import audit_infrastructure_call

    _, active = marker()

    @audit_infrastructure_call
    async def operation(value: int, *, fail: bool = False) -> int:
        await asyncio.sleep(0)
        assert active()
        if fail:
            raise RuntimeError("fixture failure")
        return value

    assert "value" in inspect.signature(operation).parameters
    assert await operation(3) == 3
    assert not active()
    with pytest.raises(RuntimeError):
        await operation(3, fail=True)
    assert not active()
