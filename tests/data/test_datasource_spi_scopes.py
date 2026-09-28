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
"""Only a singleton bean joins the datasource registry's SPI (after-begin customizers, credentials).

The registry keeps every customizer and credentials provider it is given for its whole life. Once
the BeanPostProcessors ran on every instance of every scope (C029), the SPI registrar handed it each
TRANSIENT, REQUEST and refresh-scoped instance as it was created: a request-scoped customizer that
carried its request's tenant ran in the units of work of every later request (the last one wins the
``set_config``, a cross-tenant leak), the customizer list grew by one per resolution, and a
refresh-scoped credentials provider kept answering with the password of its first, evicted
generation, so a rotation never took effect. As Spring's ``ApplicationListenerDetector`` does for
listeners, the registrar takes singletons only and logs a warning for a scoped SPI bean.

The registry runs on SQLite file databases.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402

from pyfly.container import component, service  # noqa: E402
from pyfly.container.refresh_scope import refresh_scope  # noqa: E402
from pyfly.container.types import Scope  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.refresh import ContextRefresher  # noqa: E402
from pyfly.context.request_context import RequestContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry  # noqa: E402

_APPLIED: list[str] = []
_CONSULTED: list[int] = []
_WARNING = "datasource_spi_bean_not_singleton"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    _APPLIED.clear()
    _CONSULTED.clear()
    RequestContext.clear()
    yield
    RequestContext.clear()


def _context(tmp_path: Path) -> ApplicationContext:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    return ApplicationContext(
        Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url, "ddl-auto": "none"}}}})
    )


async def _begin_unit_of_work(registry: DataSourceRegistry) -> None:
    """Begin a transaction on the primary and run its after-begin customizers, as a unit of work does."""
    async with registry.primary.engine.connect() as conn, conn.begin():
        await registry.primary.run_after_begin(conn)
        await conn.execute(text("SELECT 1"))


def _spi_warnings(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (str(getattr(record, "bean", "")), str(getattr(record, "scope", "")))
        for record in caplog.records
        if record.getMessage() == _WARNING
    ]


@component
class _AuditGuc:
    """A singleton customizer: the supported shape (it reads the tenant when the unit begins)."""

    async def after_begin(self, connection: Any, datasource: DataSource) -> None:
        del connection, datasource
        context = RequestContext.current()
        _APPLIED.append(f"audit:{context.get('tenant') if context is not None else None}")


@component(scope=Scope.TRANSIENT)
class _TransientGuc:
    async def after_begin(self, connection: Any, datasource: DataSource) -> None:
        del connection, datasource
        _APPLIED.append("transient")


@service
class _Holder:
    """A singleton that takes a transient customizer at startup (the batched post-processing pass)."""

    def __init__(self, guc: _TransientGuc) -> None:
        self.guc = guc


async def test_a_transient_customizer_is_not_registered_at_each_creation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path)
    for each in (_AuditGuc, _TransientGuc, _Holder):
        ctx.register_bean(each)
    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.auto_configuration"):
        await ctx.start()
        try:
            for _ in range(5):
                ctx.get_bean(_TransientGuc)
            registry = ctx.get_bean(DataSourceRegistry)

            assert registry.primary.customizers == (ctx.get_bean(_AuditGuc),)
            await _begin_unit_of_work(registry)
            assert _APPLIED == ["audit:None"]
        finally:
            await ctx.stop()

    # Once per class, however many instances were created (one at startup, five after it).
    assert _spi_warnings(caplog) == [(_TransientGuc.__qualname__, "transient")]


@component(scope=Scope.REQUEST)
class _RequestGuc:
    """The pattern that leaked: a request-scoped customizer carrying its request's tenant."""

    def __init__(self) -> None:
        context = RequestContext.current()
        self.tenant = context.get("tenant") if context is not None else None

    async def after_begin(self, connection: Any, datasource: DataSource) -> None:
        del connection, datasource
        _APPLIED.append(f"request:{self.tenant}")


async def test_a_request_scoped_customizer_never_runs_in_another_requests_unit_of_work(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _context(tmp_path)
    ctx.register_bean(_AuditGuc)
    ctx.register_bean(_RequestGuc)
    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.auto_configuration"):
        await ctx.start()
        try:
            registry = ctx.get_bean(DataSourceRegistry)
            for tenant in ("acme", "globex"):  # two requests, each resolving its customizer
                RequestContext.init().set("tenant", tenant)
                assert ctx.get_bean(_RequestGuc).tenant == tenant
                RequestContext.clear()

            RequestContext.init().set("tenant", "acme")  # a unit of work of acme's next request
            await _begin_unit_of_work(registry)
            RequestContext.clear()

            assert _APPLIED == ["audit:acme"]  # globex's customizer did not run for acme
            assert len(registry.primary.customizers) == 1
        finally:
            await ctx.stop()

    assert _spi_warnings(caplog) == [(_RequestGuc.__qualname__, "request")]


@refresh_scope
@component
class _RotatingCredentials:
    generations = 0

    def __init__(self) -> None:
        _RotatingCredentials.generations += 1
        self.generation = _RotatingCredentials.generations

    def datasource_credentials(self, datasource: str) -> tuple[str | None, str | None] | None:
        del datasource
        _CONSULTED.append(self.generation)
        return ("app", f"password-{self.generation}")


async def test_a_refresh_scoped_credentials_provider_is_not_consulted_after_its_eviction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _RotatingCredentials.generations = 0
    ctx = _context(tmp_path)
    ctx.register_bean(_RotatingCredentials)
    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.auto_configuration"):
        await ctx.start()
        try:
            registry = ctx.get_bean(DataSourceRegistry)
            ctx.get_bean(_RotatingCredentials)
            for _ in range(3):
                await ctx.get_bean(ContextRefresher).refresh()  # evicts the generation resolved before
                ctx.get_bean(_RotatingCredentials)
            await registry.primary.engine.dispose()
            await _begin_unit_of_work(registry)  # a new connection asks for the live credentials

            assert _RotatingCredentials.generations == 4
            assert _CONSULTED == []  # neither the evicted generations nor the live one was registered
        finally:
            await ctx.stop()

    assert _spi_warnings(caplog) == [(_RotatingCredentials.__qualname__, "refresh")]
