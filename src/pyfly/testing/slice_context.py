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
"""Functional test slices — build a minimal, started ApplicationContext for tests.

Spring's ``@WebMvcTest`` / ``@DataJpaTest`` slices, as explicit builders: each slice
registers only the beans you pass (plus collaborators supplied via ``overrides``), starts a
real context, and — for the web slice — wraps it in a :class:`PyFlyTestClient`. Missing
collaborators fail loudly through the normal ``NoSuchBeanError`` path, so a slice never
silently pulls in unrelated infrastructure; a slice that fails is stopped before the error
propagates, so its pools are closed.

A data slice checks its repositories too: a relational repository needs
``pyfly.data.relational.enabled=true`` (a document repository ``pyfly.data.document.enabled=true``),
without which its derived and ``@query`` methods are never compiled. With ``rollback=True`` every unit
of work of the test runs in a transaction that rolls back when the slice exits
(:class:`~pyfly.testing.rollback.RollbackTransaction`), so tests that share a database do not see each
other's rows. Use a SQLite file (``tmp_path``) or a server for data tests: an in-memory SQLite database
lives on one connection that every session shares.

Usage::

    async with await web_slice(UserController, overrides={UserService: fake_users}) as (ctx, client):
        client.get("/api/users").assert_status(200)

    async with await data_slice(UserRepository, config=config, rollback=True) as ctx:
        repo = ctx.get_bean(UserRepository)
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pyfly.container.exceptions import BeanCreationException
from pyfly.container.scanner import _auto_bind_interfaces
from pyfly.container.types import Scope
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.testing.rollback import RollbackTransaction


async def _build_slice(
    beans: tuple[type, ...],
    *,
    config: Config | None,
    overrides: dict[type, Any] | None,
) -> ApplicationContext:
    """Register *beans* (+ ``overrides``) into a fresh context and start it.

    The context is stopped again when its start fails, a bean cannot be resolved or a repository is not wired.
    """
    context = ApplicationContext(config or Config({}))
    for cls in beans:
        context.register_bean(cls)
        _auto_bind_interfaces(cls, context.container)  # so Protocol/ABC/port deps resolve
    for interface, impl in (overrides or {}).items():
        if isinstance(impl, type):
            # A replacement class: register it and bind the interface to it.
            context.container.register(impl, scope=Scope.SINGLETON)
            if interface is not impl:
                context.container.bind(interface, impl)
        else:
            # A pre-built instance / mock: install it directly under the interface.
            context.container.register_instance(interface, impl)
    try:
        await context.start()
        # Fail fast: resolve each slice bean now so a missing collaborator surfaces at build
        # time (matching Spring slice startup) rather than silently on first use.
        for cls in beans:
            context.get_bean(cls)
        _check_repositories(context, beans)
    except BaseException:
        await context.stop()
        raise
    return context


def _check_repositories(context: ApplicationContext, beans: Iterable[type]) -> None:
    """Refuse a repository whose data layer is off: its query methods were never compiled, and each stub would
    answer ``None`` (or run on a database the test does not know about)."""
    for base, processor, flag in _repository_layers():
        repositories = [cls for cls in beans if isinstance(cls, type) and issubclass(cls, base)]
        if repositories and not context.get_beans_of_type(processor):
            names = ", ".join(cls.__name__ for cls in repositories)
            raise BeanCreationException(
                subsystem="data",
                provider=names,
                reason=(
                    f"{names} needs the data layer the slice's configuration does not enable: set {flag}=true "
                    "(pyfly_config_for sets it for a database container). Without it the repository's derived "
                    "and @query methods are never compiled."
                ),
            )


def _repository_layers() -> list[tuple[type, type, str]]:
    """``(repository base, its post-processor, the flag that enables it)`` for each data layer installed."""
    layers: list[tuple[type, type, str]] = []
    try:
        from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
        from pyfly.data.relational.sqlalchemy.repository import Repository

        layers.append((Repository, RepositoryBeanPostProcessor, "pyfly.data.relational.enabled"))
    except ImportError:
        pass
    try:
        from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
        from pyfly.data.document.mongodb.repository import MongoRepository

        layers.append((MongoRepository, MongoRepositoryBeanPostProcessor, "pyfly.data.document.enabled"))
    except ImportError:
        pass
    return layers


class _SliceContext:
    """Async context manager yielding the started context and stopping it on exit.

    With a :class:`~pyfly.testing.rollback.RollbackTransaction`, the block runs inside it: it begins on entry
    and rolls back on exit, before the context stops.
    """

    def __init__(self, context: ApplicationContext, rollback: RollbackTransaction | None = None) -> None:
        self.context = context
        self.rollback = rollback

    async def __aenter__(self) -> ApplicationContext:
        if self.rollback is not None:
            try:
                await self.rollback.__aenter__()
            except BaseException:
                await self.context.stop()
                raise
        return self.context

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self.rollback is not None:
                await self.rollback.__aexit__(None, None, None)
        finally:
            await self.context.stop()


class _WebSliceContext:
    """Async context manager yielding ``(context, client)`` and stopping on exit."""

    def __init__(self, context: ApplicationContext, client: Any) -> None:
        self.context = context
        self.client = client

    async def __aenter__(self) -> tuple[ApplicationContext, Any]:
        return self.context, self.client

    async def __aexit__(self, *exc: object) -> None:
        await self.context.stop()


async def slice_context(
    *beans: type,
    config: Config | None = None,
    overrides: dict[type, Any] | None = None,
    rollback: bool = False,
    datasources: Iterable[str] | None = None,
) -> _SliceContext:
    """Build + start a minimal ApplicationContext containing only *beans* (and *overrides*).

    With *rollback*, every unit of work of the block runs in a transaction per datasource (the default one,
    or *datasources*) that rolls back when the block exits.
    """
    context = await _build_slice(beans, config=config, overrides=overrides)
    return _SliceContext(context, RollbackTransaction(context, datasources=datasources) if rollback else None)


async def web_slice(
    *controllers: type,
    config: Config | None = None,
    overrides: dict[type, Any] | None = None,
) -> _WebSliceContext:
    """Build a web slice: a started context with *controllers* (+ overrides) plus a test client.

    The Starlette app is built via ``create_app(context=...)`` so ``@rest_controller`` routes,
    filters, and error handlers are wired exactly as in production.
    """
    from pyfly.testing.client import PyFlyTestClient
    from pyfly.web.adapters.starlette.app import create_app

    context = await _build_slice(controllers, config=config, overrides=overrides)
    return _WebSliceContext(context, PyFlyTestClient(create_app(context=context)))


# Intent-named aliases for non-web slices (service/data layers).
service_slice = slice_context
data_slice = slice_context
