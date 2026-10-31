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

    owner = object()
    return lambda: audit_infrastructure(owner), lambda: is_audit_infrastructure(owner)


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

    from pyfly.audit._infrastructure import audit_infrastructure_call, is_audit_infrastructure

    class Owned:
        @audit_infrastructure_call
        async def operation(self, value: int, *, fail: bool = False) -> int:
            await asyncio.sleep(0)
            assert is_audit_infrastructure(self)
            if fail:
                raise RuntimeError("fixture failure")
            return value

    owned = Owned()
    assert "value" in inspect.signature(owned.operation).parameters
    assert await owned.operation(3) == 3
    assert not is_audit_infrastructure(owned)
    with pytest.raises(RuntimeError):
        await owned.operation(3, fail=True)
    assert not is_audit_infrastructure(owned)


async def test_composed_owner_decorator_targets_its_journal_instead_of_adapter():
    from pyfly.audit._infrastructure import audit_infrastructure_call, is_audit_infrastructure

    class Delivery:
        def __init__(self):
            self.journal = object()

        @audit_infrastructure_call(owner=lambda self: self.journal)
        async def deliver(self):
            await asyncio.sleep(0)
            assert is_audit_infrastructure(self.journal)
            assert not is_audit_infrastructure(self)

    delivery = Delivery()
    await delivery.deliver()
    assert not is_audit_infrastructure(delivery.journal)
