# MCP Streamable HTTP

Install `pyfly[mcp]` to serve the MCP 2026-07-28 protocol using the official Python SDK. `McpTransport` accepts dependency-injected callbacks rather than owning application tools or authorization policy. List callbacks must filter discovery for the current principal; call callbacks must authorize each operation again.

```python
from contextlib import asynccontextmanager

from mcp import types
from pyfly.mcp import McpTransport
from pyfly.web.adapters.starlette.app import create_app

async def list_tools(principal, params):
    return types.ListToolsResult(tools=[])

async def call_tool(principal, params):
    return types.CallToolResult(content=[types.TextContent(type="text", text="No tools configured")])

transport = McpTransport(
    "example", list_tools=list_tools, call_tool=call_tool,
    path="/mcp", allowed_hosts=["localhost:8000"], allowed_origins=[],
)

@asynccontextmanager
async def lifespan(app):
    # Enter the application's PyFly context lifespan here as well.
    async with transport.lifespan():
        yield

app = create_app(extra_routes=[transport.route], lifespan=lifespan)
```

Configure native security filters on the host app to authenticate all requests and set `request.state.security_context`. The transport rejects anonymous requests and makes that context available to callbacks and `SecurityContextHolder` for each request. The host is responsible for OAuth issuer and audience validation, Protected Resource Metadata and an appropriate `WWW-Authenticate` discovery challenge. Never pass an inbound token to a downstream provider. Supply explicit public host and origin allowlists and a suitable request-body limit. A manager is single-use: construct a fresh transport for a new app lifecycle. This adapter accepts only the 2026-07-28 revision; it does not provide protocol replay or 2025 compatibility.

## Portable JSON tools, resources and prompts

`McpTransport.from_server` accepts a public low-level SDK `Server` registry. Use
`pyfly.mcp.McpServer` when you want the SDK's tool, resource and prompt decorators:
its public `registry` preserves facade validation, resource metadata and MIME
types, prompts, middleware and extension interceptors. The bridge calls public
SDK APIs; it never copies SDK session dictionaries or transport objects.

```python
from pyfly.mcp import McpServer, McpTransport, McpSessionStore
from mcp.server.mcpserver import Context

server = McpServer("my-service", extensions=[my_apps_extension])

@server.tool()
async def describe_scope(ctx: Context) -> str:
    return "Authorize the current caller in your service."

transport = McpTransport.from_server(
    server.registry,
    session_store=lambda: application_context.get_bean(McpSessionStore),
    session_principal=verified_credential_binding,
    allowed_hosts=["knowledge.example.org"],
    allowed_origins=["https://knowledge.example.org"],
    max_sessions=128,
    session_idle_timeout=900,
)
```

The host's `session_principal(request, security_context)` callback returns a
`McpSessionPrincipal(owner, resource, expires_at)` from its freshly verified
credential. Use a stable grant fingerprint, canonical issuer/subject binding and
configured resource; never return an inbound bearer token as the owner or infer
the resource from untrusted forwarding headers. The host still authenticates
every HTTP request and authorizes every operation. A capability declaration is
not a permission grant. The transport does not set cookies.

Modern `2026-07-28` requests remain on the official SDK's per-request envelope
path: their capabilities come from that request, even without client info.
Legacy initialize clients receive a random session identifier. Only bounded,
credential/resource-bound initialization metadata is shared. Each subsequent
request reconstructs a fresh public SDK `Connection` and runs the public SDK
kernel with the original negotiated protocol and capabilities. Initialize on
one instance and send subsequent requests to any instance using the same shared
store and configured resource.

```yaml
pyfly:
  mcp:
    sessions:
      enabled: true
      store: sql
      datasource: primary
```

Auto-configuration supplies `McpSessionStore`; a custom bean replaces it.
`memory` is the explicit single-process alternative. Unknown backends raise;
SQL configuration never falls back to memory. `SqlMcpSessionStore` uses the
context's native datasource and primary transactions, atomic cross-instance
admission, conditional updates and deletion. Metadata cannot revive a deleted
or expired session. `max_sessions` applies across instances for the canonical
resource; keep bounds/configuration consistent during rolling revisions.

The framework schema declares `pyfly_mcp_sessions` and
`pyfly_mcp_session_admission`. Migrate these tables before enabling the adapter
in production. `ddl-auto: none`/`validate` only checks the schema; it does not
create tables. Runtime roles need SELECT/INSERT/UPDATE/DELETE on session rows and
SELECT/INSERT/UPDATE on admission rows, without DDL or admission-row deletion.
No bearer header, response payload, task or transport object is stored.

This adapter is deliberately JSON request/response only: tools, resources and
prompts, including MCP Apps capability context. Legacy GET returns 405;
DELETE removes shared initialization metadata. Unknown, expired, incompatible
or differently owned session IDs return 404 and clients perform standard
initialization again. Losing metadata never authorizes replaying an uncertain
write: preserve application idempotency and inspect existing operation state.

There is no SSE response replay, standalone notification stream, distributed
task support or server-to-client request channel. Subscriptions and Tasks
registrations are rejected, and multi-round-trip results are rejected before
request state can be released. A separately implemented MRTR deployment must
use the SDK's explicit shared `RequestStateSecurity` keys and audience; its
ephemeral single-process default is not a distributed key policy.

## Managed message subscriptions

`KafkaAdapter.subscribe_managed(topic, handler, group)` returns a handle whose `stop()` drains the group's consumer before removing that listener. If other listeners share the same topic and group, the consumer restarts with those listeners at the committed offset. Use a stable, nonempty group, configure `auto_offset_reset="earliest"` for a new group when existing records must be processed, and retain a dead-letter destination. `KafkaSecurity` configures TLS and SASL for both producer and consumers; supply credentials from a trusted secret source. Never expose those credentials in application metadata.

When an admission outcome is temporary or unknown, a handler can raise `RetainMessageError` from `pyfly.messaging.listener_container`. The broker retains the same delivery and retries with bounded backoff until it succeeds or the consumer stops; it is never dead-lettered solely because retries were exhausted. Other failures continue to use the configured finite retry and dead-letter policy. If cancellation does not stop a delivery within the shutdown budget, `stop()` raises `UnconfirmedDrainError` and retains ownership of the active consumer. Retry `stop()` once the delivery has finished; do not treat an unconfirmed stop as a completed pause.

`RabbitMQAdapter.subscribe_managed(topic, handler, group)` returns a handle whose `stop()` cancels and drains that consumer without deleting its durable queue. It can be stopped repeatedly and independently of other subscriptions. `subscribe` remains available for applications that manage all consumers through the adapter lifecycle. Both paths use native listener transaction, retry and dead-letter handling. Delivery is at least once: persist a stable message identity and its effects in the same transaction before returning from the handler. Messages without a stable publisher identity cannot safely be deduplicated by this contract.
