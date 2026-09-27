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
"""The order in which the context starts, stops and destroys the beans that use the database.

Stop used to walk the lifecycle adapters in reverse REGISTRATION order (user ``@bean`` products
first, then the auto-configurations alphabetically), so the primary engine was disposed before the
consumers, the user lifecycle beans and every ``@pre_destroy``; the writes that came after quietly
reconnected through a pool nobody disposed (C033, C101). Start had the mirror problem: a user
lifecycle bean that touched the database started before ``ddl-auto`` created the schema. Port-typed
adapters were started and stopped twice (C102), and a scanned ``@component`` with ``start()``/
``stop()`` never at all (C103).

These tests run the real relational auto-configuration, discovered from the entry points as in an
application, on a SQLite file database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Integer, String, event, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from pyfly.container import bean, component, configuration, service  # noqa: E402
from pyfly.container.exceptions import BeanCreationNotAllowedError  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.events import ContextClosedEvent, app_event_listener  # noqa: E402
from pyfly.context.lifecycle import post_construct, pre_destroy  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceRegistry  # noqa: E402
from pyfly.data.relational.sqlalchemy.entity import Base  # noqa: E402
from pyfly.kernel.lifecycle import CONSUMER_PHASE  # noqa: E402
from pyfly.scheduling.decorators import scheduled  # noqa: E402

EVENTS: list[str] = []


@pytest.fixture(autouse=True)
def _reset_events() -> Iterator[None]:
    EVENTS.clear()
    yield
    EVENTS.clear()


class _LifecycleNote(Base):
    __tablename__ = "wp07_lifecycle_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(String(50))


def _config(url: str, *, ddl_auto: str = "create") -> Config:
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, "ddl-auto": ddl_auto}}}})


async def _write(factory: async_sessionmaker[AsyncSession], body: str) -> str:
    """Insert *body* and report how it went: ``"ok"`` or the error's first line."""
    try:
        async with factory() as session, session.begin():
            session.add(_LifecycleNote(body=body))
    except Exception as exc:  # noqa: BLE001 — the test reports the failure instead of raising it
        return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    return "ok"


async def _bodies(url: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(select(_LifecycleNote.body).order_by(_LifecycleNote.id))).scalars())
    finally:
        await engine.dispose()


def _registry_state(factory: async_sessionmaker[AsyncSession]) -> str:
    from pyfly.data.relational.datasource_registry import datasource_of

    datasource = datasource_of(factory)
    registry = datasource.registry if datasource is not None else None
    return "closed" if registry is not None and registry.closed else "open"


# ---------------------------------------------------------------------------
# The beans of an application that uses the database at start, at stop and in @pre_destroy.
# ---------------------------------------------------------------------------


class _Consumer:
    """A message consumer: a lifecycle bean that takes subscriptions."""

    def subscribe(self, pattern: str, handler: Any) -> None:
        del pattern, handler

    async def start(self) -> None:
        EVENTS.append("consumer.start")

    async def stop(self) -> None:
        EVENTS.append("consumer.stop")


class _Checkpointer:
    """A user lifecycle bean that writes when it starts and when it stops."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def start(self) -> None:
        EVENTS.append(f"checkpointer.start:{await _write(self._factory, 'boot')}")

    async def stop(self) -> None:
        EVENTS.append(f"checkpointer.stop:{_registry_state(self._factory)}:{await _write(self._factory, 'final')}")


@configuration
class _AppConfiguration:
    @bean
    def consumer(self) -> _Consumer:
        return _Consumer()

    @bean
    def checkpointer(self, factory: async_sessionmaker[AsyncSession]) -> _Checkpointer:
        return _Checkpointer(factory)


@service
class _Auditor:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @app_event_listener
    async def on_closed(self, event: ContextClosedEvent) -> None:
        EVENTS.append(f"closed-event:{await _write(self._factory, 'closed-event')}")

    @pre_destroy
    async def flush(self) -> None:
        EVENTS.append(f"pre_destroy:{_registry_state(self._factory)}:{await _write(self._factory, 'pre_destroy')}")


async def test_the_database_outlives_every_bean_that_uses_it(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ctx = ApplicationContext(_config(url))
    ctx.register_bean(_AppConfiguration)
    ctx.register_bean(_Auditor)
    await ctx.start()
    engine = ctx.get_bean(AsyncEngine)
    event.listen(engine.sync_engine, "engine_disposed", lambda _engine: EVENTS.append("registry.dispose"))
    assert "checkpointer.start:ok" in EVENTS  # ddl-auto created the table before the bean started

    await ctx.stop()

    assert EVENTS == [
        "checkpointer.start:ok",
        "consumer.start",  # consumers start last and stop first
        "closed-event:ok",
        "consumer.stop",
        "pre_destroy:open:ok",
        "checkpointer.stop:open:ok",
        "registry.dispose",
    ]
    assert await _bodies(url) == ["boot", "closed-event", "pre_destroy", "final"]


@service
class _DropAuditor:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @pre_destroy
    async def flush(self) -> None:
        EVENTS.append(f"pre_destroy:{await _write(self._factory, 'pre_destroy')}")


async def test_create_drop_drops_the_schema_after_every_pre_destroy(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ctx = ApplicationContext(_config(url, ddl_auto="create-drop"))
    ctx.register_bean(_DropAuditor)
    await ctx.start()
    await ctx.stop()

    assert EVENTS == ["pre_destroy:ok"]


async def test_nothing_rebuilds_the_database_after_stop(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ctx = ApplicationContext(_config(url))
    await ctx.start()
    engine = ctx.get_bean(AsyncEngine)
    factory = ctx.get_bean(async_sessionmaker)
    await ctx.stop()

    with pytest.raises(BeanCreationNotAllowedError):
        ctx.get_bean(AsyncEngine)
    with pytest.raises(BeanCreationNotAllowedError):
        ctx.get_bean(DataSourceRegistry)
    # The engine a bean kept refuses to open a new pool instead of leaking one nobody disposes.
    with pytest.raises(Exception, match="closed"):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    assert await _write(factory, "late") != "ok"
    assert engine.pool.checkedin() == 0

    await ctx.start()  # a restart builds a new registry and a new engine
    try:
        assert ctx.get_bean(AsyncEngine) is not engine
        assert await _write(ctx.get_bean(async_sessionmaker), "restarted") == "ok"
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------
# Destruction runs in reverse dependency order, not reverse registration order.
# ---------------------------------------------------------------------------


class _Ledger:
    @pre_destroy
    def close(self) -> None:
        EVENTS.append("ledger.pre_destroy")


class _Reporter:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger

    @pre_destroy
    def close(self) -> None:
        EVENTS.append("reporter.pre_destroy")


async def test_a_bean_is_destroyed_before_the_beans_it_depends_on() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_Reporter)  # registered first, but it depends on the ledger
    ctx.register_bean(_Ledger)
    await ctx.start()
    await ctx.stop()

    assert EVENTS == ["reporter.pre_destroy", "ledger.pre_destroy"]


# ---------------------------------------------------------------------------
# Each lifecycle bean is started once and stopped once (C102, C103).
# ---------------------------------------------------------------------------


class _AuditSink:
    """A port."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _SqlAuditSink(_AuditSink):
    """An adapter that opens its engine in start(), as the Lifecycle contract says."""

    def __init__(self, url: str) -> None:
        self._url = url
        self.engines: list[AsyncEngine] = []

    async def start(self) -> None:
        EVENTS.append("sink.start")
        engine = create_async_engine(self._url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        self.engines.append(engine)

    async def stop(self) -> None:
        EVENTS.append("sink.stop")
        if self.engines:
            await self.engines[-1].dispose()


_SINK_URL: list[str] = []


@configuration
class _SinkConfiguration:
    @bean
    def audit_sink(self) -> _AuditSink:
        return _SqlAuditSink(_SINK_URL[0])


async def test_a_port_typed_adapter_is_started_and_stopped_once(tmp_path: Path) -> None:
    _SINK_URL[:] = [f"sqlite+aiosqlite:///{tmp_path / 'sink.db'}"]
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_SinkConfiguration)
    await ctx.start()
    sink = ctx.get_bean(_AuditSink)
    assert isinstance(sink, _SqlAuditSink)
    await ctx.stop()

    assert EVENTS == ["sink.start", "sink.stop"]
    assert len(sink.engines) == 1
    assert sink.engines[0].pool.checkedin() == 0


@component
class _ScannedPoller:
    async def start(self) -> None:
        EVENTS.append("poller.start")

    async def stop(self) -> None:
        EVENTS.append("poller.stop")


async def test_a_scanned_component_with_start_and_stop_is_started_and_stopped() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_ScannedPoller)
    await ctx.start()
    assert EVENTS == ["poller.start"]
    await ctx.stop()

    assert EVENTS == ["poller.start", "poller.stop"]


# ---------------------------------------------------------------------------
# A declared phase orders lifecycle beans before creation order does.
# ---------------------------------------------------------------------------


class _Phased:
    def __init__(self, name: str, phase: int) -> None:
        self.name = name
        self.phase = phase

    async def start(self) -> None:
        EVENTS.append(f"{self.name}.start")

    async def stop(self) -> None:
        EVENTS.append(f"{self.name}.stop")

    @pre_destroy
    def destroyed(self) -> None:
        EVENTS.append(f"{self.name}.pre_destroy")


@configuration
class _PhasedConfiguration:
    @bean
    def late(self) -> _Phased:
        return _Phased("late", CONSUMER_PHASE)

    @bean
    def default(self) -> _Phased:
        return _Phased("default", 0)

    @bean
    def early(self) -> _Phased:
        return _Phased("early", -10)


async def test_lifecycle_beans_start_by_ascending_phase_and_stop_by_descending_phase() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_PhasedConfiguration)
    await ctx.start()
    assert EVENTS == ["early.start", "default.start", "late.start"]
    EVENTS.clear()

    await ctx.stop()

    # The consumer-phase bean stops before any @pre_destroy (which runs in reverse creation order:
    # the methods are processed alphabetically), the others after all of them.
    assert EVENTS == [
        "late.stop",
        "late.pre_destroy",
        "early.pre_destroy",
        "default.pre_destroy",
        "default.stop",
        "early.stop",
    ]


async def test_the_registry_closes_last_whatever_the_registration_order(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An embedder (or a test) that registers the relational auto-configuration by hand puts its beans
    before the entry-point ones, so the registry's lifecycle bean is created after the engine
    lifecycle and stops before it. The registry must still close last: create-drop drops the schema
    on an open engine."""
    import logging

    from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration

    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ctx = ApplicationContext(_config(url, ddl_auto="create-drop"))
    ctx.register_bean(RelationalAutoConfiguration)
    ctx.register_bean(_DropAuditor)
    await ctx.start()
    with caplog.at_level(logging.WARNING, logger="pyfly.context.application_context"):
        await ctx.stop()

    assert EVENTS == ["pre_destroy:ok"]
    assert "adapter_stop_failed" not in caplog.text
    with pytest.raises(Exception, match="no such table"):
        await _bodies(url)


# ---------------------------------------------------------------------------
# Every bean is post-processed, wired and scheduled once, including a second bean of one class and
# a @bean registered under its port.
# ---------------------------------------------------------------------------


class _Beeper:
    """A port."""


class _ScheduledBeeper(_Beeper):
    def __init__(self, name: str) -> None:
        self.name = name

    @post_construct
    def ready(self) -> None:
        EVENTS.append(f"{self.name}.post_construct")

    @scheduled(fixed_rate=timedelta(seconds=60))
    async def beep(self) -> None:
        EVENTS.append(f"{self.name}.beep")


@configuration
class _BeeperConfiguration:
    @bean
    def port_beeper(self) -> _Beeper:
        return _ScheduledBeeper("port")

    @bean
    def first_beeper(self) -> _ScheduledBeeper:
        return _ScheduledBeeper("first")

    @bean
    def second_beeper(self) -> _ScheduledBeeper:
        return _ScheduledBeeper("second")


async def test_every_bean_is_initialized_and_scheduled_once() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_BeeperConfiguration)
    await ctx.start()
    try:
        for _ in range(50):  # the fixed-rate loops fire once right away
            if len([event for event in EVENTS if event.endswith(".beep")]) >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await ctx.stop()

    assert sorted(event for event in EVENTS if event.endswith(".post_construct")) == [
        "first.post_construct",
        "port.post_construct",
        "second.post_construct",
    ]
    assert sorted(event for event in EVENTS if event.endswith(".beep")) == ["first.beep", "port.beep", "second.beep"]
