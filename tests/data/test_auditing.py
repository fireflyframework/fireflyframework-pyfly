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
"""Entity auditing through the real ApplicationContext, filter chain and scheduler (C075, C121, C122,
C160, C167, C168).

Nothing tested ``AuditingEntityListener`` before, which let every defect below ship:

- every context start added another global listener and none was ever removed (C167, C168);
- a child-collection change turned into an ``UPDATE`` and a version bump of the parent (C121);
- there was no ``AuditorAware``: jobs, listeners and shells recorded ``created_by=None``, and an update
  with no principal kept the previous user's ``updated_by`` (C122);
- a session-authenticated user (form login, OAuth2 login, switch-user) never reached the auditor (C075).

Every test commits to a SQLite file database with foreign keys on and reads the committed rows back.
Auditing is backend-neutral (ORM events, no SQL of its own), so the SQLite lane proves it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import ForeignKey, String, create_engine, event, inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from starlette.requests import Request
from starlette.testclient import TestClient

from pyfly.container import bean, configuration, repository, rest_controller, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.request_context import RequestContext
from pyfly.core.config import Config
from pyfly.data import transactional
from pyfly.data.auditing import AuditorAware, DateTimeProvider, active_auditing_handler, run_as
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener
from pyfly.data.relational.sqlalchemy.entity import BaseEntity, VersionedMixin
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.scheduling import scheduled
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder
from pyfly.security.jwt import JWTService
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
from pyfly.security.password import BcryptPasswordEncoder
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
from pyfly.testing.statement_counter import StatementCounter
from pyfly.testing.testcontainers import pyfly_config
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filters.switch_user_filter import SwitchUserFilter
from pyfly.web.mappings import post_mapping, put_mapping, request_mapping
from pyfly.web.params import PathVar

_SECRET = "auditing-test-secret-key-at-least-32-characters"
_ENCODER = BcryptPasswordEncoder(rounds=4)


class AuditedDoc(BaseEntity):
    __tablename__ = "aud_doc"

    title: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="new")


class AuditedParent(VersionedMixin, BaseEntity):
    __tablename__ = "aud_parent"

    name: Mapped[str] = mapped_column(String(64))
    kids: Mapped[list[AuditedKid]] = relationship(back_populates="parent", lazy="selectin")


class AuditedKid(BaseEntity):
    __tablename__ = "aud_kid"

    label: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("aud_parent.id"))
    parent: Mapped[AuditedParent] = relationship(back_populates="kids", lazy="select")


_TABLES = (AuditedDoc, AuditedParent, AuditedKid)


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _create_tables(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(BaseEntity.metadata.create_all, tables=[model.__table__ for model in _TABLES])
    finally:
        await engine.dispose()


def _config(url: str, **overrides: Any) -> Config:
    return pyfly_config(
        base={
            "pyfly.data.relational.enabled": "true",
            "pyfly.data.relational.url": url,
            "pyfly.data.relational.ddl-auto": "none",
            **overrides,
        }
    )


async def _rows(url: str, model: type[BaseEntity]) -> dict[str, tuple[Any, ...]]:
    """The committed rows of *model*, read on a connection of their own: ``key -> (created_by,
    updated_by, created_at, updated_at, *extra)``."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            if model is AuditedDoc:
                stmt: Any = select(
                    AuditedDoc.title,
                    AuditedDoc.created_by,
                    AuditedDoc.updated_by,
                    AuditedDoc.created_at,
                    AuditedDoc.updated_at,
                    AuditedDoc.status,
                )
            else:
                stmt = select(
                    AuditedParent.name,
                    AuditedParent.created_by,
                    AuditedParent.updated_by,
                    AuditedParent.created_at,
                    AuditedParent.updated_at,
                    AuditedParent.version,
                )
            return {row[0]: tuple(row[1:]) for row in (await connection.execute(stmt)).all()}
    finally:
        await engine.dispose()


def _insert_hooks(model: type[BaseEntity]) -> int:
    """How many ``before_insert`` listeners the ORM runs for *model*."""
    return len(list(inspect(model).dispatch.before_insert))


@contextlib.contextmanager
def _as_request_user(user_id: str) -> Iterator[None]:
    """A RequestContext carrying *user_id*, the way the JWT and Basic filters establish it."""
    context = RequestContext.init()
    context.security_context = SecurityContext(user_id=user_id)
    try:
        yield
    finally:
        RequestContext.clear()


@pytest.fixture(autouse=True)
def _no_auditing_left_behind() -> Iterator[None]:
    yield
    assert active_auditing_handler() is None, "a test left an auditing handler registered"


# ---------------------------------------------------------------------------------------------------------
# Registration: once per process, gone when the context stops (C167, C168)
# ---------------------------------------------------------------------------------------------------------


async def test_one_hook_however_many_contexts_start_and_none_after_they_stop(tmp_path: Path) -> None:
    url = _url(tmp_path / "boots.db")
    await _create_tables(url)
    for boot in range(3):
        ctx = ApplicationContext(_config(url))
        await ctx.start()
        try:
            assert _insert_hooks(AuditedDoc) == 1, f"boot {boot + 1}"
            with _as_request_user("alice"):
                async with async_sessionmaker(ctx.get_bean(AsyncEngine))() as session, session.begin():
                    session.add(AuditedDoc(title=f"boot-{boot}"))
        finally:
            await ctx.stop()
        assert _insert_hooks(AuditedDoc) == 0

    assert {title: row[0] for title, row in (await _rows(url, AuditedDoc)).items()} == {
        "boot-0": "alice",
        "boot-1": "alice",
        "boot-2": "alice",
    }


async def test_a_stopped_context_stamps_nothing(tmp_path: Path) -> None:
    url = _url(tmp_path / "stopped.db")
    await _create_tables(url)
    ctx = ApplicationContext(_config(url))
    await ctx.start()
    await ctx.stop()

    engine = create_async_engine(url)
    try:
        with _as_request_user("ghost"):
            async with async_sessionmaker(engine)() as session, session.begin():
                session.add(AuditedDoc(title="after stop"))
    finally:
        await engine.dispose()
    assert (await _rows(url, AuditedDoc))["after stop"][0] is None


@configuration
class _ApplicationListener:
    """The documented way to register the listener by hand, beside the auto-configuration."""

    @bean
    def auditing_listener(self) -> AuditingEntityListener:
        listener = AuditingEntityListener()
        listener.register()
        return listener


async def test_an_application_listener_replaces_the_auto_configured_one(tmp_path: Path) -> None:
    url = _url(tmp_path / "own.db")
    await _create_tables(url)
    ctx = ApplicationContext(_config(url))
    ctx.register_bean(_ApplicationListener)
    await ctx.start()
    try:
        assert len(ctx.get_beans_of_type(AuditingEntityListener)) == 1
        assert _insert_hooks(AuditedDoc) == 1
    finally:
        await ctx.stop()


async def test_registering_again_changes_nothing(tmp_path: Path) -> None:
    listener = AuditingEntityListener()
    listener.register()
    listener.register()
    other = AuditingEntityListener()
    other.register()
    try:
        assert _insert_hooks(AuditedDoc) == 1
        assert active_auditing_handler() is other
        other.unregister()
        assert active_auditing_handler() is listener  # the one registered before takes over
        assert _insert_hooks(AuditedDoc) == 1
    finally:
        listener.unregister()
        other.unregister()
    assert _insert_hooks(AuditedDoc) == 0


async def test_auditing_can_be_switched_off(tmp_path: Path) -> None:
    url = _url(tmp_path / "off.db")
    await _create_tables(url)
    ctx = ApplicationContext(_config(url, **{"pyfly.data.auditing.enabled": "false"}))
    ctx.register_bean(AuditedDocRepository)
    await ctx.start()
    try:
        assert ctx.get_beans_of_type(AuditingEntityListener) == []
        assert _insert_hooks(AuditedDoc) == 0
        with _as_request_user("alice"):
            await ctx.get_bean(AuditedDocRepository).save(AuditedDoc(title="unaudited"))
    finally:
        await ctx.stop()
    assert (await _rows(url, AuditedDoc))["unaudited"][0] is None


# ---------------------------------------------------------------------------------------------------------
# A child-collection change does not update the parent (C121)
# ---------------------------------------------------------------------------------------------------------


@repository
class AuditedKidRepository(Repository[AuditedKid, uuid.UUID]):
    pass


@repository
class AuditedParentRepository(Repository[AuditedParent, uuid.UUID]):
    pass


@contextlib.asynccontextmanager
async def _audited_sessions(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], AsyncEngine]]:
    url = _url(tmp_path / "parents.db")
    await _create_tables(url)
    engine = create_async_engine(url)
    listener = AuditingEntityListener()
    listener.register()
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), engine
    finally:
        listener.unregister()
        await engine.dispose()


def _parent_updates(counter: StatementCounter) -> list[str]:
    return [s.sql for s in counter.statements if s.verb == "UPDATE" and "aud_parent" in s.sql]


async def test_adding_a_child_does_not_update_its_versioned_parent(tmp_path: Path) -> None:
    async with _audited_sessions(tmp_path) as (factory, engine):
        async with factory() as session, session.begin():
            parent = AuditedParent(name="p")
            session.add(parent)

        with StatementCounter(engine) as counter:
            async with factory() as session, session.begin():
                loaded = await session.get(AuditedParent, parent.id)
                assert loaded is not None
                loaded.kids.append(AuditedKid(label="appended"))  # through the loaded collection
            async with factory() as session, session.begin():
                loaded = await session.get(AuditedParent, parent.id)
                assert loaded is not None
                await AuditedKidRepository(session=session).save(AuditedKid(label="saved", parent=loaded))

        assert _parent_updates(counter) == []
        async with factory() as session:
            reloaded = await session.get(AuditedParent, parent.id)
            assert reloaded is not None and reloaded.version == 1 and len(reloaded.kids) == 2


async def test_concurrent_children_of_one_versioned_parent_both_commit(tmp_path: Path) -> None:
    """Each child insert used to UPDATE the versioned parent too, so the second of two overlapping inserts
    failed with StaleDataError."""
    async with _audited_sessions(tmp_path) as (factory, _engine):
        async with factory() as session, session.begin():
            parent = AuditedParent(name="p")
            session.add(parent)

        async with factory() as first, factory() as second:
            one = await first.get(AuditedParent, parent.id)
            two = await second.get(AuditedParent, parent.id)
            assert one is not None and two is not None
            one.kids.append(AuditedKid(label="one"))
            two.kids.append(AuditedKid(label="two"))
            await first.commit()
            await second.commit()

        async with factory() as session:
            kids = (await session.execute(select(AuditedKid.label).order_by(AuditedKid.label))).scalars().all()
        assert kids == ["one", "two"]


async def test_a_real_change_is_stamped_and_a_no_op_assignment_is_not(tmp_path: Path) -> None:
    async with _audited_sessions(tmp_path) as (factory, engine):
        async with factory() as session, session.begin():
            parent = AuditedParent(name="p")
            session.add(parent)
        created = parent.updated_at

        with StatementCounter(engine) as counter:
            async with factory() as session, session.begin():
                loaded = await session.get(AuditedParent, parent.id)
                assert loaded is not None
                loaded.name = loaded.name  # nothing changes
        assert _parent_updates(counter) == []

        with StatementCounter(engine) as counter, _as_request_user("bob"):
            async with factory() as session, session.begin():
                loaded = await session.get(AuditedParent, parent.id)
                assert loaded is not None
                loaded.name = "renamed"
        assert len(_parent_updates(counter)) == 1
        async with factory() as session:
            reloaded = await session.get(AuditedParent, parent.id)
        assert reloaded is not None
        assert (reloaded.name, reloaded.version, reloaded.updated_by) == ("renamed", 2, "bob")
        assert reloaded.updated_at > created


# ---------------------------------------------------------------------------------------------------------
# AuditorAware, DateTimeProvider, run_as, and updated_by (C122)
# ---------------------------------------------------------------------------------------------------------


@repository
class AuditedDocRepository(Repository[AuditedDoc, uuid.UUID]):
    async def titled(self, title: str) -> AuditedDoc | None:
        return (await self._session.execute(select(AuditedDoc).where(AuditedDoc.title == title))).scalar_one_or_none()


@service
class AuditedDocService:
    def __init__(self, docs: AuditedDocRepository) -> None:
        self._docs = docs

    @transactional
    async def create(self, title: str) -> None:
        await self._docs.save(AuditedDoc(title=title))

    @transactional
    async def set_status(self, title: str, status: str) -> None:
        doc = await self._docs.titled(title)
        assert doc is not None
        doc.status = status


class SystemFallbackAuditor(AuditorAware):
    """An application auditor: the user, or "system" when there is none."""

    def get_current_auditor(self) -> str | None:
        return SecurityContextHolder.get_authenticated_user_id() or "system"


class AsyncDirectoryAuditor(AuditorAware):
    """An ``async`` auditor (a directory lookup, say)."""

    async def get_current_auditor(self) -> str | None:
        await asyncio.sleep(0)
        user = SecurityContextHolder.get_authenticated_user_id()
        return f"directory:{user}" if user else None


FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)


class FixedClock(DateTimeProvider):
    def get_now(self) -> datetime:
        return FIXED_NOW


_AUDITOR: dict[str, AuditorAware] = {}


@configuration
class _AuditingPorts:
    @bean
    def auditor(self) -> AuditorAware:
        return _AUDITOR["auditor"]

    @bean
    def clock(self) -> DateTimeProvider:
        return FixedClock()


@contextlib.asynccontextmanager
async def _context(tmp_path: Path, *beans: type, **overrides: Any) -> AsyncIterator[tuple[ApplicationContext, str]]:
    url = _url(tmp_path / "app.db")
    await _create_tables(url)
    ctx = ApplicationContext(_config(url, **overrides))
    for bean_class in (AuditedDocRepository, AuditedDocService, *beans):
        ctx.register_bean(bean_class)
    await ctx.start()
    try:
        yield ctx, url
    finally:
        await ctx.stop()


@pytest.mark.parametrize(
    ("auditor", "anonymous", "alice"),
    [(SystemFallbackAuditor(), "system", "alice"), (AsyncDirectoryAuditor(), None, "directory:alice")],
    ids=["sync", "async"],
)
async def test_the_application_s_auditor_and_clock_stamp_entities(
    tmp_path: Path, auditor: AuditorAware, anonymous: str | None, alice: str
) -> None:
    _AUDITOR["auditor"] = auditor
    async with _context(tmp_path, _AuditingPorts) as (ctx, url):
        service_ = ctx.get_bean(AuditedDocService)
        await service_.create("anonymous")
        with _as_request_user("alice"):
            await service_.create("by alice")

    rows = await _rows(url, AuditedDoc)
    assert rows["anonymous"][:4] == (anonymous, anonymous, FIXED_NOW, FIXED_NOW)
    assert rows["by alice"][:4] == (alice, alice, FIXED_NOW, FIXED_NOW)


async def test_run_as_names_the_auditor_of_a_block_and_of_a_function(tmp_path: Path) -> None:
    async with _context(tmp_path) as (ctx, url):
        service_ = ctx.get_bean(AuditedDocService)
        with run_as("importer"):
            await service_.create("imported")
            with run_as("nested"):
                await service_.create("nested")
            await service_.create("imported again")

        @run_as("system:cleanup")
        async def cleanup() -> None:
            await service_.create("cleaned")

        await cleanup()
        await service_.create("nobody")

    rows = await _rows(url, AuditedDoc)
    assert {title: row[0] for title, row in rows.items()} == {
        "imported": "importer",
        "nested": "nested",
        "imported again": "importer",
        "cleaned": "system:cleanup",
        "nobody": None,
    }


SYSTEM_IMPORTER = run_as("system:importer")
"""One principal shared by every job that uses it, declared once at module level."""


async def test_one_run_as_instance_is_shared_by_concurrent_tasks() -> None:
    """Each task entering the shared block keeps its own restore point: before, the instance kept the
    tokens, so the first task to leave popped the other's and both raised ``ValueError``."""

    async def job(delay: float) -> tuple[str | None, str | None]:
        with SYSTEM_IMPORTER:
            await asyncio.sleep(delay)
            inside = SecurityContextHolder.get_authenticated_user_id()
        return inside, SecurityContextHolder.get_authenticated_user_id()

    results = await asyncio.gather(job(0.01), job(0.05), job(0.0))
    assert results == [("system:importer", None)] * 3
    assert SecurityContextHolder.get_context() is None

    with SYSTEM_IMPORTER:  # nested in itself, then left in order
        with SYSTEM_IMPORTER:
            assert SecurityContextHolder.get_authenticated_user_id() == "system:importer"
        assert SecurityContextHolder.get_authenticated_user_id() == "system:importer"
    assert SecurityContextHolder.get_context() is None


async def test_concurrent_jobs_sharing_a_run_as_instance_record_it(tmp_path: Path) -> None:
    async with _context(tmp_path) as (ctx, url):
        service_ = ctx.get_bean(AuditedDocService)

        async def job(title: str, delay: float) -> None:
            with SYSTEM_IMPORTER:
                await asyncio.sleep(delay)
                await service_.create(title)

        await asyncio.gather(job("first", 0.0), job("second", 0.05))  # first leaves first
        await service_.create("after")

    rows = await _rows(url, AuditedDoc)
    assert {title: row[:2] for title, row in rows.items()} == {
        "first": ("system:importer", "system:importer"),
        "second": ("system:importer", "system:importer"),
        "after": (None, None),
    }


async def test_an_update_without_a_principal_does_not_keep_the_last_user(tmp_path: Path) -> None:
    async with _context(tmp_path) as (ctx, url):
        service_ = ctx.get_bean(AuditedDocService)
        with _as_request_user("alice"):
            await service_.create("doc")
        await service_.set_status("doc", "expired")  # a job with no principal

    created_by, updated_by, created_at, updated_at, status = (await _rows(url, AuditedDoc))["doc"]
    assert (created_by, updated_by, status) == ("alice", None, "expired")
    assert updated_at > created_at


async def test_values_the_application_sets_are_kept(tmp_path: Path) -> None:
    async with _context(tmp_path) as (ctx, url):
        docs = ctx.get_bean(AuditedDocRepository)
        with _as_request_user("alice"):
            await docs.save(AuditedDoc(title="migrated", created_by="legacy-author", updated_by="legacy-editor"))

            @transactional
            async def edit_on_behalf() -> None:
                doc = await docs.titled("migrated")
                assert doc is not None
                doc.status = "edited"
                doc.updated_by = "support-agent"

            await edit_on_behalf()

    created_by, updated_by, *_rest = (await _rows(url, AuditedDoc))["migrated"]
    assert (created_by, updated_by) == ("legacy-author", "support-agent")


def test_an_async_auditor_needs_an_async_session(tmp_path: Path) -> None:
    """A synchronous ``Session`` flush cannot await an ``async`` auditor: it fails with a clear error
    instead of stamping a coroutine."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}")
    BaseEntity.metadata.create_all(engine, tables=[AuditedDoc.__table__])
    listener = AuditingEntityListener(AsyncDirectoryAuditor())
    listener.register()
    try:
        with _as_request_user("alice"), Session(engine) as session:
            session.add(AuditedDoc(title="sync"))
            with pytest.raises(TypeError, match="needs an AsyncSession"):
                session.flush()
    finally:
        listener.unregister()
        engine.dispose()


# ---------------------------------------------------------------------------------------------------------
# Through the real filter chain: bearer token, session login, switch-user, concurrent users (C075, C160)
# ---------------------------------------------------------------------------------------------------------


@rest_controller
@request_mapping("/api/docs")
class AuditedDocController:
    def __init__(self, docs: AuditedDocService) -> None:
        self._docs = docs

    @post_mapping("/{title}")
    async def create(self, request: Request, title: PathVar[str]) -> dict[str, Any]:
        await asyncio.sleep(0.02)  # let a concurrent request interleave
        await self._docs.create(title)
        web_user = getattr(getattr(request.state, "security_context", None), "user_id", None)
        return {"web_user": web_user}

    @put_mapping("/{title}/{status}")
    async def set_status(self, title: PathVar[str], status: PathVar[str]) -> dict[str, Any]:
        await self._docs.set_status(title, status)
        return {}


_USERS = InMemoryUserDetailsService(
    UserDetails(username="alice", password_hash=_ENCODER.hash("pw"), roles=["ADMIN"]),
    UserDetails(username="bob", password_hash=_ENCODER.hash("pw"), roles=["USER"]),
)


@configuration
class _SwitchUser:
    @bean
    def switch_user_filter(self) -> SwitchUserFilter:
        return SwitchUserFilter(_USERS, success_url="/")


def _web_config(url: str) -> Config:
    return _config(
        url,
        **{
            "pyfly.session.enabled": "true",
            "pyfly.security.enabled": "true",
            "pyfly.security.csrf.enabled": "false",
            "pyfly.security.jwt.secret": _SECRET,
            "pyfly.security.jwt.filter.enabled": "true",
            "pyfly.security.form-login.enabled": "true",
            "pyfly.security.form-login.use-redirect": "false",
            "pyfly.security.form-login.users.alice.password-hash": _ENCODER.hash("pw"),
            "pyfly.security.form-login.users.alice.roles": "ADMIN",
        },
    )


@contextlib.asynccontextmanager
async def _web_app(tmp_path: Path) -> AsyncIterator[tuple[Any, str]]:
    url = _url(tmp_path / "web.db")
    await _create_tables(url)
    ctx = ApplicationContext(_web_config(url))
    for bean_class in (
        AuditedDocRepository,
        AuditedDocService,
        AuditedDocController,
        OAuth2SessionSecurityFilter,  # restores the principal a form login stored in the session
        _SwitchUser,
    ):
        ctx.register_bean(bean_class)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        await ctx.start()
        yield
        await ctx.stop()

    yield create_app(context=ctx, lifespan=lifespan), url


def _bearer(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {JWTService(secret=_SECRET).encode({'sub': user, 'roles': ['USER']})}"}


async def test_a_bearer_token_user_is_recorded_on_insert_and_update(tmp_path: Path) -> None:
    async with _web_app(tmp_path) as (app, url):
        with TestClient(app) as client:
            assert client.post("/api/docs/by-carol", headers=_bearer("carol")).status_code == 200
            assert client.put("/api/docs/by-carol/reviewed", headers=_bearer("dave")).status_code == 200

    created_by, updated_by, *_rest = (await _rows(url, AuditedDoc))["by-carol"]
    assert (created_by, updated_by) == ("carol", "dave")


async def test_a_session_login_user_is_recorded(tmp_path: Path) -> None:
    """Form login stores the principal in the session; later requests restore it through
    OAuth2SessionSecurityFilter, which set only request.state, so rows got created_by=None (C075)."""
    async with _web_app(tmp_path) as (app, url):
        with TestClient(app) as client:
            login = client.post("/login", data={"username": "alice", "password": "pw"})
            assert login.status_code == 200 and login.json()["authenticated"] is True
            assert client.post("/api/docs/session-only").json() == {"web_user": "alice"}
            # A session and a bearer token: the web layer acts as the session's user, and so does auditing.
            assert client.post("/api/docs/session-and-bearer", headers=_bearer("bob")).json() == {"web_user": "alice"}

    rows = await _rows(url, AuditedDoc)
    assert rows["session-only"][:2] == ("alice", "alice")
    assert rows["session-and-bearer"][:2] == ("alice", "alice")


async def test_an_impersonated_user_is_recorded(tmp_path: Path) -> None:
    async with _web_app(tmp_path) as (app, url):
        with TestClient(app, follow_redirects=False) as client:
            assert client.post("/login", data={"username": "alice", "password": "pw"}).status_code == 200
            assert client.get("/login/impersonate", params={"username": "bob"}).status_code == 302
            assert client.post("/api/docs/as-bob").json() == {"web_user": "bob"}

    assert (await _rows(url, AuditedDoc))["as-bob"][:2] == ("bob", "bob")


async def test_concurrent_users_are_each_recorded_on_their_own_rows(tmp_path: Path) -> None:
    async with _web_app(tmp_path) as (app, url), contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        transport = httpx.ASGITransport(app=app)
        client = await stack.enter_async_context(httpx.AsyncClient(transport=transport, base_url="http://test"))
        users = [f"user-{n}" for n in range(8)]
        responses = await asyncio.gather(
            *(client.post(f"/api/docs/doc-of-{user}", headers=_bearer(user)) for user in users)
        )
        assert [response.status_code for response in responses] == [200] * len(users)

    rows = await _rows(url, AuditedDoc)
    assert {title: row[:2] for title, row in rows.items()} == {f"doc-of-{user}": (user, user) for user in users}


# ---------------------------------------------------------------------------------------------------------
# A scheduled job with a system auditor
# ---------------------------------------------------------------------------------------------------------

_JOB_RUNS: list[str] = []


@service
class ExpiryJob:
    def __init__(self, docs: AuditedDocService) -> None:
        self._docs = docs

    @scheduled(fixed_rate=timedelta(seconds=60))
    @run_as("system:expiry")
    async def expire(self) -> None:
        if _JOB_RUNS:
            return
        _JOB_RUNS.append("ran")
        await self._docs.set_status("old", "expired")
        await self._docs.create("created by the job")


async def test_a_scheduled_job_runs_as_its_system_auditor(tmp_path: Path) -> None:
    _JOB_RUNS.clear()
    url = _url(tmp_path / "job.db")
    await _create_tables(url)
    engine = create_async_engine(url)
    listener = AuditingEntityListener()
    listener.register()
    try:
        with _as_request_user("alice"):
            async with async_sessionmaker(engine)() as session, session.begin():
                session.add(AuditedDoc(title="old"))
    finally:
        listener.unregister()
        await engine.dispose()

    ctx = ApplicationContext(_config(url))
    for bean_class in (AuditedDocRepository, AuditedDocService, ExpiryJob):
        ctx.register_bean(bean_class)
    await ctx.start()
    try:
        for _ in range(200):
            if len(await _rows(url, AuditedDoc)) == 2:
                break
            await asyncio.sleep(0.02)
    finally:
        await ctx.stop()

    rows = await _rows(url, AuditedDoc)
    assert rows["old"][:2] == ("alice", "system:expiry")
    assert rows["old"][4] == "expired"
    assert rows["created by the job"][:2] == ("system:expiry", "system:expiry")


def test_listener_hooks_are_module_functions() -> None:
    """The hooks are plain functions, one per process: never a bound method of a listener, which would
    keep every listener of every stopped context alive."""
    from pyfly.data.relational.sqlalchemy import auditing

    listener = AuditingEntityListener()
    listener.register()
    try:
        assert event.contains(BaseEntity, "before_insert", auditing._before_insert)
        assert event.contains(BaseEntity, "before_update", auditing._before_update)
    finally:
        listener.unregister()
