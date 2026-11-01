# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Each native ASGI application owns transaction resolution, including late middleware."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Column, MetaData, String, Table, insert, select
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.transaction import (
    TransactionManagerRegistry,
    infrastructure_unit,
    installed_registry,
    transactional,
)


@pytest.fixture
async def contexts(tmp_path: Path) -> AsyncIterator[tuple[ApplicationContext, ApplicationContext]]:
    first, second = [
        ApplicationContext(
            Config(
                {
                    "pyfly": {
                        "data": {
                            "relational": {"enabled": True, "url": f"sqlite+aiosqlite:///{tmp_path / (name + '.db')}"}
                        },
                        "observability": {"metrics": {"enabled": False}},
                        "server": {"observability": {"enabled": False}},
                        "web": {"request-logging": {"enabled": False}},
                    }
                }
            )
        )
        for name in ("first", "second")
    ]
    await first.start()
    await second.start()
    try:
        yield first, second
    finally:
        await second.stop()
        await first.stop()


def app_for(adapter: str, context: ApplicationContext, routes=None):
    if adapter == "fastapi":
        from pyfly.web.adapters.fastapi.app import create_app
    else:
        from pyfly.web.adapters.starlette.app import create_app
    return create_app(context=context, docs_enabled=False, actuator_enabled=False, extra_routes=routes or [])


def owned_write(context: ApplicationContext, table: Table):
    @transactional
    async def write(label: str) -> None:
        async with infrastructure_unit(context.get_bean(DataSourceRegistry).primary) as session:
            await session.execute(insert(table).values(label=label))

    return write


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
async def test_two_apps_bind_their_whole_late_middleware_and_controller(contexts, adapter: str) -> None:
    first, second = contexts
    table = Table("scope_labels", MetaData(), Column("label", String, primary_key=True))
    apps = []
    for name, context in (("a", first), ("b", second)):
        async with context.get_bean(DataSourceRegistry).primary.engine.begin() as conn:
            await conn.run_sync(table.create)
        write = owned_write(context, table)

        def build(owner, label, call):
            async def endpoint(request):
                await call(label + "-route")
                return JSONResponse({"owner": label})

            class LateMiddleware:
                def __init__(self, app):
                    self.app = app

                async def __call__(self, scope, receive, send):
                    assert installed_registry() is owner.get_bean(TransactionManagerRegistry)
                    await call(label + "-middleware-" + scope["path"].strip("/"))
                    if scope["path"] == "/mcp":
                        await JSONResponse({"owner": label})(scope, receive, send)
                        return
                    await self.app(scope, receive, send)
                    assert installed_registry() is owner.get_bean(TransactionManagerRegistry)

            app = app_for(adapter, owner, [Route("/unit", endpoint)])
            app.add_middleware(LateMiddleware)
            return app

        apps.append(build(context, name, write))

    async def request(app, path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://owned") as client:
            return await client.get(path)

    replies = await asyncio.gather(request(apps[0], "/unit"), request(apps[1], "/unit"))
    assert [r.status_code for r in replies] == [200, 200]
    # The late middleware short-circuits before native routing, as an MCP HTTP boundary may do.
    reply = await request(apps[0], "/mcp")
    assert reply.json() == {"owner": "a"}
    for name, context in (("a", first), ("b", second)):
        async with context.get_bean(DataSourceRegistry).primary.engine.connect() as conn:
            expected = {name + "-route", name + "-middleware-unit"}
            if name == "a":
                expected.add("a-middleware-mcp")
            assert set((await conn.execute(select(table.c.label))).scalars()) == expected
    assert installed_registry() is second.get_bean(TransactionManagerRegistry)


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
async def test_stream_cancellation_resets_caller_registry(contexts, adapter: str) -> None:
    first, second = contexts
    first_registry = first.get_bean(TransactionManagerRegistry)
    closed = asyncio.Event()

    async def endpoint(request):
        async def chunks():
            try:
                assert installed_registry() is first_registry
                yield b"owned"
                await asyncio.Event().wait()
            finally:
                closed.set()

        return StreamingResponse(chunks())

    app = app_for(adapter, first, [Route("/stream", endpoint)])
    sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        assert installed_registry() is first_registry
        sent.append(message)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "path": "/stream",
                    "raw_path": b"/stream",
                    "root_path": "",
                    "query_string": b"",
                    "headers": [],
                    "scheme": "http",
                    "server": ("owned", 80),
                    "client": ("owned", 1),
                },
                receive,
                send,
            )
    assert any(m["type"] == "http.response.body" for m in sent)
    assert closed.is_set()
    assert installed_registry() is second.get_bean(TransactionManagerRegistry)


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
async def test_websocket_uses_the_owning_registry(contexts, adapter: str) -> None:
    first, second = contexts

    async def endpoint(websocket):
        assert installed_registry() is first.get_bean(TransactionManagerRegistry)
        await websocket.accept()
        await websocket.send_text("owned")
        await websocket.close()

    app = app_for(adapter, first)
    app.router.routes.append(WebSocketRoute("/ws", endpoint))
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "path": "/ws",
            "raw_path": b"/ws",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "scheme": "ws",
            "server": ("owned", 80),
            "client": ("owned", 1),
            "subprotocols": [],
        },
        receive,
        send,
    )
    assert any(message.get("text") == "owned" for message in sent)
    assert installed_registry() is second.get_bean(TransactionManagerRegistry)


def test_registry_scopes_mask_none_nest_and_reset_on_error() -> None:
    from pyfly.data.transaction import bind_registry
    from pyfly.data.transaction.registry import install_registry, uninstall_registry

    first, second = TransactionManagerRegistry(), TransactionManagerRegistry()
    install_registry(second)
    try:
        with bind_registry(first):
            assert installed_registry() is first
            with pytest.raises(RuntimeError), bind_registry(None):
                assert installed_registry() is None
                raise RuntimeError("owned")
            assert installed_registry() is first
        assert installed_registry() is second
    finally:
        uninstall_registry(second)


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
async def test_an_owned_app_without_data_masks_another_apps_registry(contexts, adapter: str) -> None:
    from pyfly.data.transaction import TransactionTemplate
    from pyfly.data.transaction.errors import IllegalTransactionStateError
    from pyfly.data.transaction.registry import install_registry

    _, second = contexts
    no_data = ApplicationContext(Config({"pyfly": {"observability": {"metrics": {"enabled": False}}}}))
    await no_data.start()
    own_registry = no_data.get_bean(TransactionManagerRegistry)
    install_registry(second.get_bean(TransactionManagerRegistry))
    try:
        app = app_for(adapter, no_data)

        class LateMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                assert installed_registry() is own_registry
                with pytest.raises(IllegalTransactionStateError, match="No transaction manager"):
                    async with TransactionTemplate().transaction():
                        pytest.fail("Borrowed another application's Data")
                await JSONResponse({"isolated": True})(scope, receive, send)

        app.add_middleware(LateMiddleware)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://owned") as client:
            assert (await client.get("/none")).json() == {"isolated": True}
        assert installed_registry() is second.get_bean(TransactionManagerRegistry)
    finally:
        await no_data.stop()


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
async def test_late_middleware_error_restores_caller_binding(contexts, adapter: str) -> None:
    first, second = contexts
    app = app_for(adapter, first)

    class LateMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            assert installed_registry() is first.get_bean(TransactionManagerRegistry)
            raise RuntimeError("owned failure")

    app.add_middleware(LateMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://owned"
    ) as client:
        assert (await client.get("/error")).status_code == 500
    assert installed_registry() is second.get_bean(TransactionManagerRegistry)


@pytest.mark.parametrize("base_path", ["", "/ops"])
async def test_management_outer_boundary_owns_late_middleware_and_mounted_routes(contexts, base_path: str) -> None:
    from pyfly.web.adapters.starlette.management_app import create_management_app

    first, second = contexts
    app = create_management_app(
        first,
        health_agg=None,
        http_exchange_recorder=None,
        admin_trace_collector=None,
        actuator_active=False,
        admin_enabled=False,
        base_path=base_path,
    )

    async def endpoint(request):
        assert installed_registry() is first.get_bean(TransactionManagerRegistry)
        return JSONResponse({"owner": "first"})

    target = app.routes[0].app if base_path else app
    target.router.routes.append(Route("/probe", endpoint))

    class LateMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            assert installed_registry() is first.get_bean(TransactionManagerRegistry)
            await self.app(scope, receive, send)

    app.add_middleware(LateMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://owned") as client:
        assert (await client.get(base_path + "/probe")).json() == {"owner": "first"}
    assert installed_registry() is second.get_bean(TransactionManagerRegistry)


async def test_binding_another_registry_cannot_join_or_bypass_an_active_unit(contexts) -> None:
    from pyfly.data.transaction import TransactionTemplate, bind_registry
    from pyfly.data.transaction.errors import IllegalTransactionStateError, UnexpectedRollbackError

    first, second = contexts
    with pytest.raises(UnexpectedRollbackError), bind_registry(first.get_bean(TransactionManagerRegistry)):
        async with TransactionTemplate().transaction() as owner:
            with (
                bind_registry(second.get_bean(TransactionManagerRegistry)),
                pytest.raises(IllegalTransactionStateError, match="different transaction manager"),
            ):
                async with TransactionTemplate().transaction():
                    pytest.fail("Cross-context transaction joined")
            assert owner is not None and not owner.completed
    assert installed_registry() is second.get_bean(TransactionManagerRegistry)


def test_request_binding_does_not_import_database_vendors() -> None:
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import asyncio, importlib.abc, sys
        class DenyVendors(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in {'sqlalchemy', 'pymongo', 'beanie'}:
                    raise AssertionError('A request binding imported a database vendor')
        sys.meta_path.insert(0, DenyVendors())
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from pyfly.container.exceptions import NoSuchBeanError
        from pyfly.data.transaction import TransactionManagerRegistry, installed_registry
        from pyfly.data.transaction.registry import install_registry
        from pyfly.web.adapters.starlette.context_bound import ContextBoundStarlette
        fallback = TransactionManagerRegistry()
        install_registry(fallback)
        class NoData:
            def get_bean(self, kind):
                raise NoSuchBeanError(bean_type=kind)
        async def endpoint(request):
            assert installed_registry() is None
            return PlainTextResponse('owned')
        app = ContextBoundStarlette(pyfly_context=NoData(), routes=[Route('/', endpoint)])
        async def receive(): return {'type':'http.request','body':b'','more_body':False}
        async def send(message): pass
        scope = {'type':'http','method':'GET','path':'/','root_path':'','query_string':b'','headers':[]}
        asyncio.run(app(scope, receive, send))
        assert installed_registry() is fallback
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
