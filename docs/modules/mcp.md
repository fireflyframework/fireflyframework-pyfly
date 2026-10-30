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

## Managed message subscriptions

`KafkaAdapter.subscribe_managed(topic, handler, group)` returns a handle whose `stop()` drains the group's consumer before removing that listener. If other listeners share the same topic and group, the consumer restarts with those listeners at the committed offset. Use a stable, nonempty group, configure `auto_offset_reset="earliest"` for a new group when existing records must be processed, and retain a dead-letter destination. `KafkaSecurity` configures TLS and SASL for both producer and consumers; supply credentials from a trusted secret source. Never expose those credentials in application metadata.

When an admission outcome is temporary or unknown, a handler can raise `RetainMessageError` from `pyfly.messaging.listener_container`. The broker retains the same delivery and retries with bounded backoff until it succeeds or the consumer stops; it is never dead-lettered solely because retries were exhausted. Other failures continue to use the configured finite retry and dead-letter policy. If cancellation does not stop a delivery within the shutdown budget, `stop()` raises `UnconfirmedDrainError` and retains ownership of the active consumer. Retry `stop()` once the delivery has finished; do not treat an unconfirmed stop as a completed pause.

`RabbitMQAdapter.subscribe_managed(topic, handler, group)` returns a handle whose `stop()` cancels and drains that consumer without deleting its durable queue. It can be stopped repeatedly and independently of other subscriptions. `subscribe` remains available for applications that manage all consumers through the adapter lifecycle. Both paths use native listener transaction, retry and dead-letter handling. Delivery is at least once: persist a stable message identity and its effects in the same transaction before returning from the handler. Messages without a stable publisher identity cannot safely be deduplicated by this contract.
