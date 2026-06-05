# WebSocket Guide

Add real-time, bidirectional WebSocket endpoints to your controllers with the
`@websocket_mapping` decorator and a clean, framework-agnostic session API.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Quick Example](#quick-example)
3. [The @websocket_mapping Decorator](#the-websocket_mapping-decorator)
4. [WebSocketSession](#websocketsession)
   - [Connection Metadata](#connection-metadata)
   - [Lifecycle and Messaging](#lifecycle-and-messaging)
5. [The WebSocketHandler Protocol](#the-websockethandler-protocol)
6. [Lifecycle Hooks: on_disconnect](#lifecycle-hooks-on_disconnect)
7. [Route Discovery](#route-discovery)
8. [Disconnect and Exception Handling](#disconnect-and-exception-handling)
9. [Complete Example](#complete-example)
10. [See Also](#see-also)

---

## Introduction

REST is request/response, but some features — chat, live dashboards, streaming
notifications — need a persistent, bidirectional channel. The PyFly WebSocket
module lets you declare WebSocket endpoints the same way you declare HTTP
endpoints: a decorated method on a controller bean.

The module mirrors the `@get_mapping` / `@post_mapping` pattern from the web
layer:

- **`@websocket_mapping`** marks a controller method as a WebSocket endpoint.
- **`WebSocketSession`** is a framework-agnostic wrapper over the raw
  connection, providing a clean async API for accepting, sending, receiving,
  and closing.
- **`WebSocketHandler`** is an optional protocol describing lifecycle hooks
  (`on_connect`, `on_message`, `on_disconnect`).
- **`WebSocketRegistrar`** (Starlette adapter) discovers `@websocket_mapping`
  methods and builds Starlette `WebSocketRoute` objects with lazy bean
  resolution.

Public types are available from a single import:

```python
from pyfly.websocket import (
    WebSocketHandler,
    WebSocketSession,
    websocket_mapping,
)
```

WebSocket routes are auto-discovered — no extra configuration is needed. The
web server's route collection runs `WebSocketRegistrar` alongside HTTP and SSE
route discovery.

---

## Quick Example

A simple echo endpoint on a controller:

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping
from pyfly.websocket import WebSocketSession, websocket_mapping


@rest_controller
@request_mapping("/ws")
class EchoController:

    @websocket_mapping("/echo")
    async def echo(self, session: WebSocketSession) -> None:
        await session.accept()
        while True:
            msg = await session.receive_text()
            await session.send_text(f"echo: {msg}")
```

This exposes a WebSocket endpoint at `/ws/echo`.

---

## The @websocket_mapping Decorator

`@websocket_mapping(path="")` marks a controller method as a WebSocket
endpoint. It mirrors the HTTP mapping decorators but flags the method as a
WebSocket handler rather than an HTTP route.

```python
from pyfly.websocket import websocket_mapping


@websocket_mapping("/chat")
async def handle_chat(self, session: WebSocketSession) -> None:
    ...
```

- The decorated method must accept a single `WebSocketSession` argument.
- The method manages the **full connection lifecycle** — accept, message loop,
  and close.
- The full route path is the controller's `@request_mapping` base path
  concatenated with the decorator's `path`.

Under the hood the decorator attaches `__pyfly_ws_mapping__ = {"path": path}`
to the method, which the registrar reads during route discovery.

---

## WebSocketSession

`WebSocketSession` is a framework-agnostic wrapper around the raw WebSocket
connection. It is currently backed by Starlette's `WebSocket`, but the public
API avoids leaking implementation details.

### Connection Metadata

| Property | Returns | Description |
|---|---|---|
| `path_params` | `dict[str, Any]` | Path parameters extracted from the WebSocket URL |
| `query_params` | mapping | Query parameters from the WebSocket URL |
| `headers` | mapping | Request headers from the WebSocket handshake |

```python
@websocket_mapping("/rooms/{room_id}")
async def join(self, session: WebSocketSession) -> None:
    room_id = session.path_params["room_id"]
    await session.accept()
    ...
```

### Lifecycle and Messaging

| Method | Description |
|---|---|
| `await accept(subprotocol=None)` | Accept the WebSocket connection handshake |
| `await send_text(data)` | Send a text message to the client |
| `await send_json(data, mode="text")` | Send a JSON-serializable object |
| `await send_bytes(data)` | Send binary data |
| `await receive_text()` | Receive a text message (`str`) |
| `await receive_json(mode="text")` | Receive and decode a JSON message |
| `await receive_bytes()` | Receive binary data (`bytes`) |
| `await close(code=1000, reason=None)` | Close the connection |

```python
@websocket_mapping("/feed")
async def feed(self, session: WebSocketSession) -> None:
    await session.accept()
    msg = await session.receive_json()
    await session.send_json({"received": msg})
    await session.close()
```

---

## The WebSocketHandler Protocol

`WebSocketHandler` is an optional `@runtime_checkable` protocol describing
lifecycle hooks.

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class WebSocketHandler(Protocol):
    async def on_connect(self, session: WebSocketSession) -> None:
        """Called when a client initiates a connection (not yet accepted)."""

    async def on_message(self, session: WebSocketSession, data: str) -> None:
        """Called when a text message is received from the client."""

    async def on_disconnect(self, session: WebSocketSession) -> None:
        """Called when the WebSocket connection is closed."""
```

In `on_connect` the handshake is **not** yet accepted — call
`await session.accept()` to complete it.

**Automatic invocation:** Of these three hooks, only `on_disconnect` is
automatically invoked by the Starlette adapter's registrar (in a `finally`
block after the handler returns or raises). `on_connect` and `on_message` are
part of the protocol for structural typing purposes and must be called
explicitly from within the `@websocket_mapping` handler method if your
controller uses them.

---

## Lifecycle Hooks: on_disconnect

If the controller defines an `on_disconnect` method, the registrar invokes it
after the handler finishes — whether it returned normally, the client
disconnected, or the handler raised. This gives handlers a place to clean up
(remove the session from a room, release resources, etc.):

```python
@rest_controller
@request_mapping("/ws")
class ChatController:

    def __init__(self) -> None:
        self._sessions: set[WebSocketSession] = set()

    @websocket_mapping("/chat")
    async def chat(self, session: WebSocketSession) -> None:
        await session.accept()
        self._sessions.add(session)
        while True:
            msg = await session.receive_text()
            await session.send_text(msg)

    async def on_disconnect(self, session: WebSocketSession) -> None:
        self._sessions.discard(session)
```

`on_disconnect` may be sync or async — the registrar awaits it when it returns
an awaitable. Exceptions raised by `on_disconnect` are suppressed so cleanup
never masks the original outcome.

---

## Route Discovery

`WebSocketRegistrar` (the Starlette adapter) discovers WebSocket endpoints and
builds routes, following the same lazy bean-resolution pattern used for HTTP
routes:

1. Iterates over container registrations with the `rest_controller` or
   `controller` stereotype.
2. Reads each class's `@request_mapping` base path.
3. Finds methods carrying `__pyfly_ws_mapping__` (from `@websocket_mapping`).
4. Creates a Starlette `WebSocketRoute` at `base_path + mapping_path`.

Controller bean resolution is **deferred until the first WebSocket
connection** — the same lazy pattern as HTTP routes — so beans produced later
in startup are still wired correctly.

**Source:** `src/pyfly/websocket/adapters/starlette.py`

---

## Disconnect and Exception Handling

The Starlette endpoint wraps each handler invocation so connection lifecycle is
handled robustly:

- A `WebSocketDisconnect` raised by the client is caught and treated as a
  normal close — your message loop does not need to catch it explicitly.
- Any other exception raised by the handler is **logged** (via
  `logging.warning` with a traceback) rather than swallowed silently.
- The `on_disconnect` hook, if present, always runs in a `finally` block.

---

## Complete Example

A minimal broadcast chat controller:

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping
from pyfly.websocket import WebSocketSession, websocket_mapping


@rest_controller
@request_mapping("/ws")
class ChatController:
    """Broadcasts each received message to every connected client."""

    def __init__(self) -> None:
        self._clients: set[WebSocketSession] = set()

    @websocket_mapping("/chat")
    async def chat(self, session: WebSocketSession) -> None:
        await session.accept()
        self._clients.add(session)
        try:
            while True:
                msg = await session.receive_text()
                for client in list(self._clients):
                    await client.send_text(msg)
        finally:
            self._clients.discard(session)

    async def on_disconnect(self, session: WebSocketSession) -> None:
        # Belt-and-suspenders cleanup; also invoked by the registrar.
        self._clients.discard(session)
```

The endpoint is reachable at `ws://<host>/ws/chat`.

---

## See Also

- [Web Layer](web.md) — `@rest_controller`, `@request_mapping`, HTTP routing
- [Server Module](server.md) — ASGI server and event-loop selection
- [Events (EDA)](events.md) — Application events for connection lifecycle
