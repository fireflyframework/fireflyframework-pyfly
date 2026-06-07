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
silently pulls in unrelated infrastructure.

Usage::

    async with await web_slice(UserController, overrides={UserService: fake_users}) as (ctx, client):
        client.get("/api/users").assert_status(200)

    async with await data_slice(UserRepository) as ctx:
        repo = ctx.get_bean(UserRepository)
"""

from __future__ import annotations

from typing import Any

from pyfly.container.scanner import _auto_bind_interfaces
from pyfly.container.types import Scope
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


async def _build_slice(
    beans: tuple[type, ...],
    *,
    config: Config | None,
    overrides: dict[type, Any] | None,
) -> ApplicationContext:
    """Register *beans* (+ ``overrides``) into a fresh context and start it."""
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
            context.container.register(interface, scope=Scope.SINGLETON)
            context.container._registrations[interface].instance = impl
    await context.start()
    # Fail fast: resolve each slice bean now so a missing collaborator surfaces at build
    # time (matching Spring slice startup) rather than silently on first use.
    for cls in beans:
        context.get_bean(cls)
    return context


class _SliceContext:
    """Async context manager yielding the started context and stopping it on exit."""

    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    async def __aenter__(self) -> ApplicationContext:
        return self.context

    async def __aexit__(self, *exc: object) -> None:
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
) -> _SliceContext:
    """Build + start a minimal ApplicationContext containing only *beans* (and *overrides*)."""
    return _SliceContext(await _build_slice(beans, config=config, overrides=overrides))


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
