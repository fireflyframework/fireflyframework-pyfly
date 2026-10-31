# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native HTTP/security hooks commit host-authoritative facts without capturing request payloads."""

import asyncio
import secrets
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from pyfly.audit import AuditActor, AuditContext, AuditContextResolver, AuditJournal, AuditResource, AuditScope
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.audit.recording_auto_configuration import AuditRecordingAutoConfiguration
from pyfly.audit.recording_binding import AuditModulePostProcessor
from pyfly.container import bean, configuration
from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.context import ApplicationContext
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder
from pyfly.security.http_security import HttpSecurity
from pyfly.security.jwt import JWTService
from pyfly.security.oauth2.resource_server import JWKSTokenValidator
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
from pyfly.session.session import HttpSession
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import OAuth2ResourceServerFilter
from pyfly.web.adapters.starlette.filters.security_filter import SecurityFilter
from pyfly.web.auto_configuration import WebAutoConfiguration
from pyfly.web.filters import OncePerRequestFilter

pytestmark = pytest.mark.backends("sqlite-file", "pg")


@asynccontextmanager
async def application(
    backend,
    *,
    project="project",
    enabled=True,
    authentication="jwt",
    oauth_error_mode="401",
    session_present=True,
    handler_function=None,
    web_adapter="starlette",
):
    scope = AuditScope(tenant_id="tenant", workspace_id="workspace", project_id=project)
    calls, operations = [], []
    jwt = JWTService(secret=secrets.token_hex(32))

    class Resolver:
        broken = False

        async def resolve(self, operation):
            if self.broken:
                raise RuntimeError("PRIVATE fixture resolver detail")
            operations.append(operation)
            verified = SecurityContextHolder.get_context()
            actor = "verified-actor" if verified and verified.is_authenticated else "host-security-boundary"
            return AuditContext(
                scope=scope,
                actor=AuditActor(kind="service", subject_id=actor),
                resource=AuditResource(kind=operation.resource_kind or "request", resource_id=f"{project}-endpoint"),
            )

    resolver = Resolver()

    @configuration
    class Authority:
        @bean
        def trusted_audit_context(self) -> AuditContextResolver:
            return resolver

    context = ApplicationContext(
        backend.config(
            {
                "pyfly.data.relational.ddl-auto": "create",
                "pyfly.audit.enabled": "true",
                "pyfly.audit.recording.enabled": "true" if enabled else "false",
                "pyfly.audit.recording.modules": ["web", "security"],
                "pyfly.web.request-logging.enabled": "false",
            }
        ),
        auto_configurations=[
            DataSourceAutoConfiguration,
            RelationalAutoConfiguration,
            AuditAutoConfiguration,
            AuditRecordingAutoConfiguration,
            WebAutoConfiguration,
        ],
    )
    context.register_bean(Authority)
    if authentication == "jwt":
        context.container.register_instance(SecurityFilter, SecurityFilter(jwt))
    elif authentication == "oauth2":

        class Validator(JWKSTokenValidator):
            def __init__(self):
                pass

            def to_security_context(self, token):
                return jwt.to_security_context(token)

        context.container.register_instance(
            OAuth2ResourceServerFilter, OAuth2ResourceServerFilter(Validator(), error_mode=oauth_error_mode)
        )
    elif authentication == "session":

        class SessionFixture(OncePerRequestFilter):
            __pyfly_order__ = HIGHEST_PRECEDENCE + 210

            async def do_filter(self, request, call_next):
                if session_present:
                    request.state.session = HttpSession(
                        "PRIVATE-session-id",
                        {
                            "SECURITY_CONTEXT": SecurityContext(
                                user_id="PRIVATE-session-user", permissions=["fixture:execute"]
                            )
                        },
                    )
                return await call_next(request)

        context.container.register_instance(SessionFixture, SessionFixture())
        context.container.register_instance(OAuth2SessionSecurityFilter, OAuth2SessionSecurityFilter())
    rules = HttpSecurity()
    rules.authorize_requests().request_matchers("/private").has_permission("fixture:execute")
    authorization = rules.build()
    context.container.register_instance(type(authorization), authorization)

    async def handler(request):
        calls.append(True)
        if handler_function is not None:
            return await handler_function(request)
        return PlainTextResponse("fixture body")

    await context.start()
    factory = create_app
    if web_adapter == "fastapi":
        from pyfly.web.adapters.fastapi.app import create_app as fastapi_app

        factory = fastapi_app
    app = factory(
        context=context, docs_enabled=False, actuator_enabled=False, extra_routes=[Route("/private", handler)]
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://fixture"
        ) as client:
            yield context, client, jwt, scope, calls, resolver, operations
    finally:
        await context.stop()


@pytest.mark.parametrize("web_adapter", ["starlette", "fastapi"])
async def test_native_di_http_allow_deny_records_only_authoritative_static_facts(relational_backend, web_adapter):
    async with application(relational_backend, web_adapter=web_adapter) as values:
        context, client, jwt, scope, calls, resolver, operations = values
        token = jwt.encode({"sub": "PRIVATE-unchecked-subject", "permissions": ["fixture:execute"]})
        response = await client.get(
            "/private?secret=PRIVATE-query",
            headers={"authorization": f"Bearer {token}", "x-request-id": "PRIVATE-header"},
        )
        assert response.status_code == 200 and calls == [True]
        entries = await context.get_bean(AuditJournal).read(scope)
        assert [(item.event.event_type, item.event.outcome) for item in entries] == [
            ("security.authenticate", "admitted"),
            ("web.request", "started"),
            ("security.authorize", "admitted"),
            ("web.request", "observed"),
        ]
        assert all(item.event.actor.subject_id == "verified-actor" for item in entries)
        assert b"PRIVATE" not in b"".join(item.to_bytes() for item in entries)
        assert all(operation.resource_kind for operation in operations)
        response = await client.get("/private")
        assert response.status_code == 401 and calls == [True]
        tail = (await context.get_bean(AuditJournal).read(scope))[len(entries) :]
        assert [(item.event.event_type, item.event.outcome) for item in tail] == [
            ("web.request", "started"),
            ("security.authorize", "denied"),
            ("web.request", "observed"),
        ]
        assert all(item.event.actor.subject_id == "host-security-boundary" for item in tail)
        token = jwt.encode({"sub": "fixture", "permissions": []})
        response = await client.get("/private", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 403 and calls == [True]
        denied = (await context.get_bean(AuditJournal).read(scope))[-4:]
        assert denied[-2].event.event_type == "security.authorize" and denied[-2].event.outcome == "denied"
        assert all(item.event.actor.subject_id == "verified-actor" for item in denied)
        coverage = context.get_bean(AuditModulePostProcessor).coverage()
        names = {item.adapter.rsplit(".", 1)[-1] for item in coverage.di_bound_adapters}
        assert {"AuditWebFilter", "SecurityFilter", "HttpSecurityFilter"} <= names


@pytest.mark.parametrize("failure", ["context", "journal"])
async def test_mandatory_native_capture_failure_prevents_http_handler(relational_backend, monkeypatch, failure):
    async with application(relational_backend) as (context, client, jwt, scope, calls, resolver, operations):
        if failure == "context":
            resolver.broken = True
        else:

            async def fail_append(event):
                raise RuntimeError("PRIVATE fixture journal detail")

            monkeypatch.setattr(context.get_bean(AuditJournal), "append", fail_append)
        token = jwt.encode({"sub": "fixture", "permissions": ["fixture:execute"]})
        response = await client.get("/private", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 500 and not calls
        assert not await context.get_bean(AuditJournal).read(scope)
        assert "PRIVATE" not in response.text


async def test_disabled_native_capture_preserves_existing_http_authority(relational_backend):
    async with application(relational_backend, enabled=False) as (
        context,
        client,
        jwt,
        scope,
        calls,
        resolver,
        operations,
    ):
        token = jwt.encode({"sub": "fixture", "permissions": ["fixture:execute"]})
        assert (await client.get("/private", headers={"authorization": f"Bearer {token}"})).status_code == 200
        assert (await client.get("/private")).status_code == 401
        assert calls == [True] and not operations
        assert not await context.get_bean(AuditJournal).read(scope)


async def test_two_native_contexts_do_not_share_hook_actor_scope_or_journal(relational_backend):
    async with (
        application(relational_backend, project="one") as first,
        application(relational_backend, project="two") as second,
    ):

        async def invoke(values):
            context, client, jwt, scope, calls, resolver, operations = values
            token = jwt.encode({"sub": "fixture", "permissions": ["fixture:execute"]})
            assert (await client.get("/private", headers={"authorization": f"Bearer {token}"})).status_code == 200
            entries = await context.get_bean(AuditJournal).read(scope)
            assert len(entries) == 4
            assert all(
                item.event.scope == scope and item.event.resource.resource_id == f"{scope.project_id}-endpoint"
                for item in entries
            )

        await asyncio.gather(invoke(first), invoke(second))


@pytest.mark.parametrize("authentication", ["oauth2", "session"])
async def test_native_oauth2_and_restored_session_capture_uses_verified_host_context(
    relational_backend, authentication
):
    async with application(relational_backend, authentication=authentication) as values:
        context, client, jwt, scope, calls, resolver, operations = values
        token = jwt.encode({"sub": "PRIVATE-oauth-subject", "permissions": ["fixture:execute"]})
        assert (await client.get("/private", headers={"authorization": f"Bearer {token}"})).status_code == 200
        entries = await context.get_bean(AuditJournal).read(scope)
        first = entries[0].event
        assert first.event_type == (
            "security.session_restore" if authentication == "session" else "security.authenticate"
        )
        assert first.outcome == "admitted" and first.actor.subject_id == "verified-actor"
        assert len(entries) == 4 and calls == [True]
        assert b"PRIVATE" not in b"".join(item.to_bytes() for item in entries)
        names = {
            item.adapter.rsplit(".", 1)[-1]
            for item in context.get_bean(AuditModulePostProcessor).coverage().di_bound_adapters
        }
        assert ("OAuth2SessionSecurityFilter" if authentication == "session" else "OAuth2ResourceServerFilter") in names


@pytest.mark.parametrize("authentication,error_mode", [("jwt", "401"), ("oauth2", "401"), ("oauth2", "anonymous")])
async def test_invalid_credentials_are_captured_before_native_denial_without_trusting_identity(
    relational_backend, authentication, error_mode
):
    async with application(relational_backend, authentication=authentication, oauth_error_mode=error_mode) as values:
        context, client, jwt, scope, calls, resolver, operations = values
        assert (
            await client.get("/private", headers={"authorization": "Bearer PRIVATE-unverified-token"})
        ).status_code == 401
        assert not calls
        entries = await context.get_bean(AuditJournal).read(scope)
        assert entries[0].event.event_type == "security.authenticate" and entries[0].event.outcome == "denied"
        assert all(item.event.actor.subject_id == "host-security-boundary" for item in entries)
        assert b"PRIVATE" not in b"".join(item.to_bytes() for item in entries)
        assert len(entries) == (1 if authentication == "oauth2" and error_mode == "401" else 4)


async def test_absent_session_is_not_an_invented_authentication_denial(relational_backend):
    async with application(relational_backend, authentication="session", session_present=False) as values:
        context, client, jwt, scope, calls, resolver, operations = values
        assert (await client.get("/private")).status_code == 401
        entries = await context.get_bean(AuditJournal).read(scope)
        assert [item.event.event_type for item in entries] == ["web.request", "security.authorize", "web.request"]
        assert not calls


@pytest.mark.parametrize("authentication", ["jwt", "oauth2"])
async def test_valid_token_without_established_principal_is_not_reported_authenticated(
    relational_backend, authentication
):
    async with application(relational_backend, authentication=authentication) as values:
        context, client, jwt, scope, calls, resolver, operations = values
        token = jwt.encode({"permissions": ["fixture:execute"]})
        assert (await client.get("/private", headers={"authorization": f"Bearer {token}"})).status_code == 401
        first = (await context.get_bean(AuditJournal).read(scope))[0].event
        assert first.event_type == "security.authenticate" and first.outcome == "denied"
        assert first.actor.subject_id == "host-security-boundary" and not calls


@pytest.mark.parametrize("terminal", ["complete", "exception", "cancel"])
async def test_stream_frames_remain_incremental_and_dispatch_outcome_is_not_delivery_success(
    relational_backend, terminal
):
    from starlette.responses import StreamingResponse

    first, release = asyncio.Event(), asyncio.Event()
    frames = []

    async def handler(request):
        async def content():
            yield b"first"
            await release.wait()
            if terminal == "exception":
                raise RuntimeError("PRIVATE downstream fixture")
            yield b"last"

        return StreamingResponse(content(), status_code=207, media_type="text/event-stream")

    async with application(relational_backend, handler_function=handler) as values:
        context, client, jwt, scope, calls, resolver, operations = values
        token = jwt.encode({"sub": "fixture", "permissions": ["fixture:execute"]})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            frames.append(dict(message))
            if message["type"] == "http.response.body" and message.get("body") == b"first":
                first.set()

        request_scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/private",
            "raw_path": b"/private",
            "query_string": b"secret=PRIVATE-query",
            "root_path": "",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "server": ("fixture", 80),
            "client": ("127.0.0.1", 1),
        }
        pending = asyncio.create_task(client._transport.app(request_scope, receive, send))
        try:
            await asyncio.wait_for(first.wait(), 2)
            assert not pending.done()
            assert frames[0]["status"] == 207
            before = await context.get_bean(AuditJournal).read(scope)
            assert len(before) == 3 and before[1].event.outcome == "started"
            if terminal == "cancel":
                pending.cancel("PRIVATE cancellation detail")
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await pending
                assert not cancelled.value.args
            else:
                release.set()
                if terminal == "exception":
                    with pytest.raises(RuntimeError):
                        await pending
                else:
                    await pending
            entries = await context.get_bean(AuditJournal).read(scope)
            assert entries[-1].event.event_type == "web.request"
            assert entries[-1].event.outcome == ("observed" if terminal == "complete" else "unknown")
            assert not any(item.event.outcome == "succeeded" for item in entries)
            assert b"PRIVATE" not in b"".join(item.to_bytes() for item in entries)
            assert b"".join(frame.get("body", b"") for frame in frames) == (
                b"firstlast" if terminal == "complete" else b"first"
            )
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending


async def test_http_capture_refuses_dispatch_inside_a_native_data_unit(relational_backend):
    from pyfly.data.relational.sqlalchemy import SqlAlchemyTransactionManager
    from pyfly.data.transaction import TransactionTemplate

    async with application(relational_backend) as values:
        context, client, jwt, scope, calls, resolver, operations = values
        token = jwt.encode({"sub": "fixture", "permissions": ["fixture:execute"]})
        async with TransactionTemplate(context.get_bean(SqlAlchemyTransactionManager)).transaction(read_only=True):
            assert (await client.get("/private", headers={"authorization": f"Bearer {token}"})).status_code == 500
        assert not calls and not await context.get_bean(AuditJournal).read(scope)
