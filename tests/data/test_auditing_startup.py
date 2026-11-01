# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""A failed native refresh must never publish an unowned entity-auditing handler."""

import asyncio

import pytest

from pyfly.container import bean, configuration
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError, NoUniqueBeanError
from pyfly.data.auditing import active_auditing_handler, registered_auditing_handlers
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder
from tests.audit.test_recording_configuration import HostResolver, OtherHostResolver, application


@pytest.mark.parametrize("failure", ["missing", "ambiguous", "modules"])
async def test_failed_configuration_does_not_publish_entity_auditing(tmp_path, failure):
    before = registered_auditing_handlers()
    context = application(
        tmp_path / "failed.db", resolver=failure != "missing", modules=["unsupported"] if failure == "modules" else None
    )
    if failure == "ambiguous":
        context.register_bean(OtherHostResolver)
    error = {"missing": NoSuchBeanError, "ambiguous": NoUniqueBeanError, "modules": BeanCreationException}[failure]
    with pytest.raises(error):
        await context.start()
    assert registered_auditing_handlers() == before
    await context.stop()
    assert registered_auditing_handlers() == before


async def test_failed_configuration_restarts_with_one_owned_handler_and_no_retained_principal(tmp_path):
    before = registered_auditing_handlers()
    context = application(tmp_path / "restart.db", resolver=False)
    with pytest.raises(NoSuchBeanError), SecurityContextHolder.using(SecurityContext(user_id="failed-start-actor")):
        await context.start()
    assert registered_auditing_handlers() == before
    context.register_bean(HostResolver)
    await context.start()
    try:
        handler = context.get_bean(AuditingEntityListener)
        assert registered_auditing_handlers() == (*before, handler)
        assert await handler.current_auditor() is None
        with SecurityContextHolder.using(SecurityContext(user_id="current-server-actor")):
            assert await handler.current_auditor() == "current-server-actor"
        assert await handler.current_auditor() is None
    finally:
        await context.stop()
    assert registered_auditing_handlers() == before


async def test_failed_refresh_cannot_stamp_later_unowned_sql_with_a_server_principal(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.data.test_auditing import AuditedDoc, _create_tables, _rows

    assert not registered_auditing_handlers()
    context = application(tmp_path / "failed.db", resolver=False)
    with pytest.raises(NoSuchBeanError):
        await context.start()
    url = f"sqlite+aiosqlite:///{tmp_path / 'unowned.db'}"
    await _create_tables(url)
    engine = create_async_engine(url)
    try:
        with SecurityContextHolder.using(SecurityContext(user_id="unowned-server-principal")):
            async with async_sessionmaker(engine)() as session, session.begin():
                session.add(AuditedDoc(title="after-failed-refresh"))
    finally:
        await engine.dispose()
    assert (await _rows(url, AuditedDoc))["after-failed-refresh"][:2] == (None, None)
    assert not registered_auditing_handlers()


async def test_non_lifo_context_stop_and_failed_refresh_preserve_the_other_owner(tmp_path):
    before = registered_auditing_handlers()
    first, second = application(tmp_path / "first.db"), application(tmp_path / "second.db")
    await first.start()
    await second.start()
    try:
        first_handler, second_handler = first.get_bean(AuditingEntityListener), second.get_bean(AuditingEntityListener)
        assert registered_auditing_handlers() == (*before, first_handler, second_handler)
        failed = application(tmp_path / "failed.db", resolver=False)
        with pytest.raises(NoSuchBeanError):
            await failed.start()
        assert registered_auditing_handlers() == (*before, first_handler, second_handler)
        await first.stop()
        assert registered_auditing_handlers() == (*before, second_handler)
        assert active_auditing_handler() is second_handler
        with SecurityContextHolder.using(SecurityContext(user_id="remaining-owner-actor")):
            assert await second_handler.current_auditor() == "remaining-owner-actor"
        assert await second_handler.current_auditor() is None
    finally:
        await second.stop()
        await first.stop()
    assert registered_auditing_handlers() == before


async def test_cancellation_before_listener_start_leaves_no_registered_handler(tmp_path):
    before = registered_auditing_handlers()
    entered = asyncio.Event()

    class EarlyResource:
        phase = -(1 << 29)

        async def start(self):
            entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            pass

    @configuration
    class Resources:
        @bean
        def early_resource(self) -> EarlyResource:
            return EarlyResource()

    context = application(tmp_path / "cancelled.db")
    context.register_bean(Resources)
    task = asyncio.create_task(context.start())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registered_auditing_handlers() == before
    await context.stop()
    assert registered_auditing_handlers() == before
