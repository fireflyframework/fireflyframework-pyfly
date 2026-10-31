# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Process roles can disable schedules without disabling explicit async dispatch."""

import asyncio
from datetime import timedelta

import pytest

from pyfly.container import service
from pyfly.container.exceptions import BeanCreationException
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.scheduling import async_method, scheduled


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [False, "false", "0", 0])
async def test_disabled_context_does_not_run_or_discover_scheduled_jobs(setting):
    fired = asyncio.Event()

    @service
    class Consumer:
        @scheduled(fixed_delay=timedelta(milliseconds=1))
        async def tick(self):
            fired.set()

    context = ApplicationContext(Config({"pyfly": {"scheduling": {"enabled": setting}}}))
    context.register_bean(Consumer)
    await context.start()
    try:
        await asyncio.sleep(0.025)
        assert not fired.is_set()
        assert context._wiring_counts.get("scheduled", 0) == 0
    finally:
        await context.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [None, True, "true"])
async def test_default_and_enabled_context_run_the_scheduled_consumer(setting):
    fired = asyncio.Event()

    @service
    class Consumer:
        @scheduled(fixed_delay=timedelta(milliseconds=1))
        async def tick(self):
            fired.set()

    values = {} if setting is None else {"pyfly": {"scheduling": {"enabled": setting}}}
    context = ApplicationContext(Config(values))
    context.register_bean(Consumer)
    await context.start()
    try:
        await asyncio.wait_for(fired.wait(), 1)
    finally:
        await context.stop()


@pytest.mark.asyncio
async def test_disabled_schedules_keep_explicit_async_methods_available():
    fired = asyncio.Event()

    @service
    class Explicit:
        @async_method
        async def dispatch(self):
            fired.set()

    context = ApplicationContext(Config({"pyfly": {"scheduling": {"enabled": False}}}))
    context.register_bean(Explicit)
    await context.start()
    try:
        await context.get_bean(Explicit).dispatch()
        await asyncio.wait_for(fired.wait(), 1)
    finally:
        await context.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["sometimes", [], {}, 2])
async def test_invalid_scheduling_switch_fails_startup(value):
    context = ApplicationContext(Config({"pyfly": {"scheduling": {"enabled": value}}}))
    with pytest.raises(BeanCreationException, match="scheduling.enabled") as failure:
        await context.start()
    assert isinstance(failure.value.__cause__, ValueError)
