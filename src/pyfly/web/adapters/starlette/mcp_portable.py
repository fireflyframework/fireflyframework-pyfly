# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""SDK-owned modern transport and portable legacy JSON initialization metadata."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
from mcp import types
from mcp.server.connection import Connection
from mcp.server.lowlevel import Server
from mcp.server.runner import serve_one
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from mcp.shared.exceptions import MCPError, NoBackChannelError
from mcp.shared.message import ServerMessageMetadata
from mcp.shared.transport_context import TransportContext
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pyfly.kernel import UnauthorizedException
from pyfly.mcp.sessions import McpSessionPrincipal, McpSessionStore
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder


@dataclass
class _JsonExchange:
    request_id: Any
    message_metadata: ServerMessageMetadata
    transport: TransportContext
    can_send_request: bool = False
    progress_token: Any = None
    cancel_requested: anyio.Event = field(default_factory=anyio.Event)

    async def send_raw_request(self, method: str, params: Any, opts: Any = None) -> Any:
        raise NoBackChannelError(method)

    async def notify(self, method: str, params: Any, opts: Any = None) -> None:
        return None

    async def progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        return None


class PortableMcpTransport:
    """JSON tools/resources/prompts with shared legacy initialization metadata.

    Modern envelopes stay on the SDK's official transport. Legacy connections
    are rebuilt through its public kernel for each independently authenticated
    request; no stream, task, response, or SDK transport object is persisted.
    """

    def __init__(
        self,
        server: Server[Any],
        *,
        session_store: McpSessionStore | Callable[[], McpSessionStore],
        session_principal: Callable[..., Any],
        allowed_hosts: Sequence[str],
        allowed_origins: Sequence[str] = (),
        path: str = "/mcp",
        max_request_body_size: int = 65536,
        max_sessions: int = 128,
        session_idle_timeout: int = 900,
    ) -> None:
        if not path.startswith("/") or path == "/" or path.endswith("/"):
            raise ValueError("MCP path must be an absolute non-root path without a trailing slash")
        if not allowed_hosts or min(max_request_body_size, max_sessions, session_idle_timeout) <= 0:
            raise ValueError("MCP requires explicit hosts and positive bounds")
        for method in (
            "subscriptions/listen",
            "tasks/get",
            "tasks/update",
            "tasks/result",
            "tasks/list",
            "tasks/cancel",
            "resources/subscribe",
            "resources/unsubscribe",
        ):
            if server.get_request_handler(method) is not None:
                raise ValueError(f"Portable JSON MCP does not support {method}")
        self.server, self.principal = server, session_principal
        self._store_source = session_store
        self._resolved_store: McpSessionStore | None = None
        self.max_request_body_size, self.max_sessions, self.ttl = (
            max_request_body_size,
            max_sessions,
            session_idle_timeout,
        )
        self.security = TransportSecurityMiddleware(
            TransportSecuritySettings(allowed_hosts=list(allowed_hosts), allowed_origins=list(allowed_origins))
        )
        self._state: Any = None
        self._running = False

        async def complete_only(ctx: Any, call_next: Any) -> Any:
            result = await call_next(ctx)
            if isinstance(result, types.InputRequiredResult) or (
                isinstance(result, dict) and result.get("resultType") == "input_required"
            ):
                raise MCPError(types.INVALID_REQUEST, "Multi-round-trip requests are outside portable JSON MCP")
            return result

        server.middleware.insert(0, complete_only)
        original_lifespan = server.lifespan

        @asynccontextmanager
        async def lifespan(instance: Server[Any]) -> AsyncIterator[Any]:
            async with original_lifespan(instance) as state:
                self._state, self._running = state, True
                try:
                    yield state
                finally:
                    self._state, self._running = None, False

        server.lifespan = lifespan
        self._app = server.streamable_http_app(
            streamable_http_path=path,
            json_response=True,
            stateless_http=True,
            max_request_body_size=max_request_body_size,
            max_sessions=max_sessions,
            session_idle_timeout=session_idle_timeout,
            transport_security=self.security.settings,
        )
        self.route = Route(path, self)

    @property
    def store(self) -> McpSessionStore:
        """Resolve the host's DI bean once, after its application context has started."""
        if self._resolved_store is None:
            target = self._store_source() if callable(self._store_source) else self._store_source
            if not isinstance(target, McpSessionStore):
                raise TypeError("MCP transport requires an explicit metadata store")
            self._resolved_store = target
        return self._resolved_store

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        start = getattr(self.store, "start", None)
        if start is not None:
            await start()
        async with self.server.session_manager.run():
            yield

    @staticmethod
    def _error(identifier: Any, code: int, message: str, status: int = 400, data: Any = None) -> JSONResponse:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return JSONResponse({"jsonrpc": "2.0", "id": identifier, "error": error}, status_code=status)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        context = getattr(request.state, "security_context", None)
        if not isinstance(context, SecurityContext) or not context.is_authenticated:
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        if not self._running:
            raise RuntimeError("MCP transport lifespan is not running")
        single_headers = {
            b"authorization",
            b"host",
            b"origin",
            b"mcp-session-id",
            b"mcp-protocol-version",
            b"mcp-method",
            b"mcp-name",
        }
        seen = set()
        for name, _ in scope.get("headers", ()):
            name = name.lower()
            if name in single_headers:
                if name in seen:
                    await self._error(None, types.INVALID_REQUEST, "Repeated routing or authentication header")(
                        scope, receive, send
                    )
                    return
                seen.add(name)
        invalid = await self.security.validate_request(request, is_post=request.method == "POST")
        if invalid is not None:
            await invalid(scope, receive, send)
            return
        if request.method not in {"POST", "DELETE"}:
            await Response(status_code=405, headers={"Allow": "POST, DELETE"})(scope, receive, send)
            return
        if request.method == "DELETE" and request.headers.get("mcp-protocol-version") not in {
            None,
            *HANDSHAKE_PROTOCOL_VERSIONS,
        }:
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return
        try:
            principal = self.principal(request, context)
            if inspect.isawaitable(principal):
                principal = await principal
        except UnauthorizedException:
            principal = None
        if not isinstance(principal, McpSessionPrincipal) or principal.expires_at <= datetime.now(UTC):
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        if request.method == "DELETE":
            found = await self.store.delete(request.headers.get("mcp-session-id", ""), principal)
            await Response(status_code=204 if found else 404)(scope, receive, send)
            return
        body = bytearray()
        async for block in request.stream():
            body.extend(block)
            if len(body) > self.max_request_body_size:
                await Response(status_code=413)(scope, receive, send)
                return
        try:
            decoded = json.loads(body)
        except ValueError:
            await self._error(None, types.PARSE_ERROR, "Parse error")(scope, receive, send)
            return
        version = request.headers.get("mcp-protocol-version")
        meta = (
            (decoded.get("params") or {}).get("_meta", {})
            if isinstance(decoded, dict) and isinstance(decoded.get("params", {}), dict)
            else {}
        )
        if (
            isinstance(meta, dict)
            and types.PROTOCOL_VERSION_META_KEY in meta
            and (version is None or version in HANDSHAKE_PROTOCOL_VERSIONS)
        ):
            await self._error(
                decoded.get("id"), types.INVALID_REQUEST, "Modern metadata requires its matching HTTP protocol version"
            )(scope, receive, send)
            return
        if (
            (version is not None and version not in HANDSHAKE_PROTOCOL_VERSIONS)
            or isinstance(meta, dict)
            and types.PROTOCOL_VERSION_META_KEY in meta
        ):
            delivered = False

            async def replay() -> Any:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            with SecurityContextHolder.using(context):
                await self._app(scope, replay, send)
            return
        try:
            message = (
                types.JSONRPCRequest.model_validate(decoded)
                if isinstance(decoded, dict) and "id" in decoded
                else types.JSONRPCNotification.model_validate(decoded)
            )
            response = await self._legacy(request, message, principal, context)
        except ValidationError:
            response = self._error(
                decoded.get("id") if isinstance(decoded, dict) else None,
                types.INVALID_PARAMS,
                "Invalid request parameters",
            )
        except MCPError as error:
            response = self._error(decoded.get("id"), error.code, error.message, 200, error.data)
        await response(scope, receive, send)

    async def _legacy(
        self, request: Request, message: Any, principal: McpSessionPrincipal, context: SecurityContext
    ) -> Response:
        session_id = request.headers.get("mcp-session-id")
        dctx = _JsonExchange(
            getattr(message, "id", None),
            ServerMessageMetadata(request_context=request),
            TransportContext(kind="streamable-http", can_send_request=False, headers=request.headers),
        )
        if message.method == "initialize":
            if not isinstance(message, types.JSONRPCRequest):
                return self._error(None, types.INVALID_REQUEST, "Initialize requires a request ID")
            if session_id:
                return self._error(message.id, types.INVALID_REQUEST, "Initialize without a session ID")
            connection = Connection.for_loop(dctx, protocol_version_hint=LATEST_HANDSHAKE_VERSION)
            with SecurityContextHolder.using(context):
                result = await serve_one(
                    self.server, dctx, message.method, message.params, connection=connection, lifespan_state=self._state
                )
            assert connection.client_params is not None
            identifier = await self.store.create(
                principal,
                connection.protocol_version,
                connection.client_params.model_dump(mode="json", by_alias=True),
                ttl=self.ttl,
                max_sessions=self.max_sessions,
            )
            if identifier is None:
                return self._error(message.id, types.INTERNAL_ERROR, "MCP session capacity reached", 503)
            return JSONResponse(
                {"jsonrpc": "2.0", "id": message.id, "result": result}, headers={"Mcp-Session-Id": identifier}
            )
        if not session_id:
            return self._error(getattr(message, "id", None), types.INVALID_REQUEST, "MCP session required")
        row = await self.store.get(session_id, principal, ttl=self.ttl)
        if row is None:
            return self._error(getattr(message, "id", None), types.INVALID_REQUEST, "Session not found", 404)
        version = request.headers.get("mcp-protocol-version")
        if version is not None and version != row.protocol_version:
            return self._error(getattr(message, "id", None), types.INVALID_REQUEST, "Session protocol mismatch")
        if message.method == "notifications/initialized":
            found = await self.store.initialize(session_id, principal)
            return Response(status_code=202 if found else 404)
        if isinstance(message, types.JSONRPCNotification):
            return Response(status_code=202)
        if not row.initialized:
            return self._error(message.id, types.INVALID_REQUEST, "MCP initialization is incomplete")
        params = types.InitializeRequestParams.model_validate(row.client_params)
        connection = Connection.from_envelope(
            row.protocol_version,
            params.client_info.model_dump(mode="json", by_alias=True),
            params.capabilities.model_dump(mode="json", by_alias=True),
        )
        with SecurityContextHolder.using(context):
            result = await serve_one(
                self.server, dctx, message.method, message.params, connection=connection, lifespan_state=self._state
            )
        return JSONResponse({"jsonrpc": "2.0", "id": message.id, "result": result})
