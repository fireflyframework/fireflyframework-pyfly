# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Bound admission before durable claims; cancellation must return assigned permits."""

import asyncio

import pytest

from pyfly.core.config import Config
from pyfly.kernel.exceptions import BulkheadException
from pyfly.resilience import QueuedBulkhead, ResilienceRegistry


async def queued(pool, count):
    async with asyncio.timeout(2):
        while pool.waiting_count != count:
            await asyncio.sleep(0)


async def test_waiters_are_fifo_and_cannot_be_bypassed_by_new_arrivals():
    pool = QueuedBulkhead(max_concurrent=1, max_waiters=2)
    await pool.acquire()
    first = asyncio.create_task(pool.acquire())
    await queued(pool, 1)
    second = asyncio.create_task(pool.acquire())
    await queued(pool, 2)
    with pytest.raises(BulkheadException):
        await pool.acquire()
    pool.release()
    await first
    assert not second.done() and pool.active_count == 1
    third = asyncio.create_task(pool.acquire())
    await queued(pool, 2)
    pool.release()
    await second
    assert not third.done()
    pool.release()
    await third
    pool.release()
    assert pool.available_slots == 1 and pool.waiting_count == 0


@pytest.mark.parametrize("assigned", [False, True])
async def test_cancelled_waiter_never_leaks_a_permit(assigned):
    pool = QueuedBulkhead(max_concurrent=1, max_waiters=2)
    await pool.acquire()
    task = asyncio.create_task(pool.acquire())
    await queued(pool, 1)
    if assigned:
        pool.release()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if not assigned:
        pool.release()
    async with pool.hold():
        assert pool.active_count == 1
    assert pool.active_count == 0 and pool.waiting_count == 0


async def test_timeout_removes_waiter_without_releasing_someone_elses_permit():
    pool = QueuedBulkhead(max_concurrent=1, max_waiters=1, wait_timeout_seconds=0.01)
    await pool.acquire()
    with pytest.raises(BulkheadException, match="timed out"):
        await pool.acquire()
    assert pool.active_count == 1 and pool.waiting_count == 0
    pool.release()
    async with pool.hold():
        pass


async def test_close_rejects_waiters_and_new_calls_without_canceling_active_work():
    pool = QueuedBulkhead(max_concurrent=1, max_waiters=2)
    await pool.acquire()
    tasks = [asyncio.create_task(pool.acquire()) for _ in range(2)]
    await queued(pool, 2)
    pool.close()
    pool.close()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(value, BulkheadException) for value in outcomes)
    assert pool.closed and pool.active_count == 1 and pool.waiting_count == 0
    with pytest.raises(BulkheadException):
        await pool.acquire()
    pool.release()
    assert pool.active_count == 0


async def test_hold_releases_on_worker_failure():
    pool = QueuedBulkhead(max_concurrent=1)
    with pytest.raises(ValueError):
        async with pool.hold():
            raise ValueError("handler failed")
    assert pool.available_slots == 1
    with pytest.raises(RuntimeError, match="permit"):
        pool.release()


async def test_zero_waiters_preserves_fail_fast_admission():
    pool = QueuedBulkhead(max_concurrent=1, max_waiters=0)
    async with pool.hold():
        with pytest.raises(BulkheadException):
            await pool.acquire()


def test_instance_cannot_be_shared_between_event_loops():
    pool = QueuedBulkhead(max_concurrent=1)

    async def use():
        async with pool.hold():
            pass

    asyncio.run(use())
    with pytest.raises(RuntimeError, match="event loop"):
        asyncio.run(use())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrent": 0},
        {"max_concurrent": True},
        {"max_concurrent": 1.5},
        {"max_waiters": -1},
        {"max_waiters": True},
        {"max_waiters": 1.5},
        {"wait_timeout_seconds": 0},
        {"wait_timeout_seconds": True},
        {"wait_timeout_seconds": float("inf")},
        {"wait_timeout_seconds": float("nan")},
    ],
)
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        QueuedBulkhead(**kwargs)


async def test_registry_binds_named_admission_limits():
    registry = ResilienceRegistry.from_config(
        Config(
            {
                "pyfly": {
                    "resilience": {
                        "queued-bulkhead": {
                            "documents": {"max-concurrent": 1, "max-waiters": 4, "wait-timeout": "250ms"},
                        }
                    }
                }
            }
        )
    )
    pool = registry.queued_bulkhead("documents")
    assert pool.max_concurrent == 1 and pool.max_waiters == 4
    assert pool.wait_timeout_seconds == 0.25
    assert registry.queued_bulkhead_names == ["documents"]
    assert registry.queued_bulkhead("documents") is pool
    with pytest.raises(KeyError):
        registry.queued_bulkhead("missing")


async def test_registry_shutdown_closes_pending_admission():
    from pyfly.context import ApplicationContext
    from pyfly.resilience.auto_configuration import ResilienceAutoConfiguration

    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "banner": {"mode": "off"},
                    "resilience": {
                        "queued-bulkhead": {"jobs": {"max-concurrent": 1, "max-waiters": 1}},
                    },
                }
            }
        )
    )
    context.register_bean(ResilienceAutoConfiguration)
    await context.start()
    pool = context.get_bean(ResilienceRegistry).queued_bulkhead("jobs")
    await pool.acquire()
    waiter = asyncio.create_task(pool.acquire())
    await queued(pool, 1)
    await context.stop()
    with pytest.raises(BulkheadException):
        await asyncio.wait_for(waiter, timeout=1)
    assert pool.closed and pool.active_count == 1
    pool.release()


@pytest.mark.parametrize("placeholder", [False, True])
def test_registry_resolves_deployment_limits(monkeypatch, placeholder):
    monkeypatch.setenv("WORKER_LIMIT", "2")
    monkeypatch.setenv("PYFLY_RESILIENCE_QUEUED_BULKHEAD_JOBS_MAX_WAITERS", "3")
    monkeypatch.setenv("PYFLY_RESILIENCE_QUEUED_BULKHEAD_JOBS_WAIT_TIMEOUT", "200ms")
    if not placeholder:
        monkeypatch.setenv("PYFLY_RESILIENCE_QUEUED_BULKHEAD_JOBS_MAX_CONCURRENT", "2")
    config = Config(
        {
            "pyfly": {
                "resilience": {
                    "queued-bulkhead": {
                        "jobs": {
                            "max-concurrent": "${WORKER_LIMIT:1}" if placeholder else 10,
                            "max-waiters": 0,
                            "wait-timeout": "30s",
                        }
                    }
                }
            }
        }
    )
    pool = ResilienceRegistry.from_config(config).queued_bulkhead("jobs")
    assert pool.max_concurrent == 2
    assert pool.max_waiters == 3
    assert pool.wait_timeout_seconds == 0.2


@pytest.mark.parametrize("value", ["true", "1.5", "0", "-1", "nan"])
def test_registry_rejects_invalid_deployment_concurrency(monkeypatch, value):
    monkeypatch.setenv("PYFLY_RESILIENCE_QUEUED_BULKHEAD_JOBS_MAX_CONCURRENT", value)
    config = Config({"pyfly": {"resilience": {"queued-bulkhead": {"jobs": {"max-concurrent": 10}}}}})
    with pytest.raises(ValueError):
        ResilienceRegistry.from_config(config)


def test_registry_overrides_omitted_settings_for_a_declared_instance(monkeypatch):
    monkeypatch.setenv("PYFLY_RESILIENCE_QUEUED_BULKHEAD_JOBS_MAX_CONCURRENT", "1")
    monkeypatch.setenv("PYFLY_RESILIENCE_QUEUED_BULKHEAD_JOBS_MAX_WAITERS", "2")
    monkeypatch.setenv("PYFLY_RESILIENCE_QUEUED_BULKHEAD_JOBS_WAIT_TIMEOUT", "100ms")
    config = Config({"pyfly": {"resilience": {"queued-bulkhead": {"jobs": {}}}}})
    pool = ResilienceRegistry.from_config(config).queued_bulkhead("jobs")
    assert pool.max_concurrent == 1
    assert pool.max_waiters == 2
    assert pool.wait_timeout_seconds == 0.1
