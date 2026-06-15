# FastAPI Adapter

> **Module:** Web -- [Module Guide](../modules/web.md)
> **Package:** `pyfly.web.adapters.fastapi`
> **Backend:** FastAPI 0.115+, Starlette 0.40+

## Quick Start

### Installation

```bash
uv add "pyfly[fastapi]"
```

Or install the full high-performance FastAPI stack (FastAPI + Granian + uvloop):

```bash
uv add "pyfly[web-fastapi]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  web:
    adapter: auto
  server:
    port: 8080
    host: "0.0.0.0"
```

No explicit adapter setting is needed. When FastAPI is installed alongside Starlette, the FastAPI adapter is auto-selected because it has higher priority.

### Minimal Example

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping, get_mapping

@rest_controller
@request_mapping("/api/hello")
class HelloController:
    @get_mapping("")
    async def hello(self) -> dict:
        return {"message": "Hello, World!"}
```

```bash
pyfly run --reload
# http://localhost:8080/api/hello
# http://localhost:8080/docs      (Swagger UI -- native FastAPI)
# http://localhost:8080/redoc     (ReDoc -- native FastAPI)
```

---

## What is FastAPI in PyFly?

FastAPI is a first-class web adapter in PyFly, sitting alongside the Starlette adapter as a peer implementation of `WebServerPort`. It provides the same controller registration, parameter binding, filter chain, and exception handling as the Starlette adapter, with the addition of FastAPI's native OpenAPI integration.

Key characteristics:

- **Same programming model** -- `@rest_controller`, `@get_mapping`, `Body[T]`, `Valid[T]`, and all other PyFly web decorators work identically
- **Native OpenAPI** -- FastAPI generates OpenAPI schemas natively from its route definitions, providing richer `/docs` and `/redoc` endpoints
- **Drop-in replacement** -- No controller changes needed when switching from Starlette to FastAPI
- **Higher-priority auto-detection** -- When both FastAPI and Starlette are installed, the FastAPI adapter is preferred

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.web.adapter` | `str` | `"auto"` | Adapter selection (`auto`, `fastapi`, `starlette`) |
| `pyfly.server.port` | `int` | `8080` | Application server port |
| `pyfly.server.host` | `str` | `"0.0.0.0"` | Server bind address |
| `pyfly.management.server.port` | `int` | `9090` | Management port serving actuator + admin separately (env `PYFLY_MANAGEMENT_SERVER_PORT`) |
| `pyfly.web.debug` | `bool` | `false` | Debug mode |
| `pyfly.web.docs.enabled` | `bool` | `true` | Enable OpenAPI / Swagger UI / ReDoc |
| `pyfly.web.actuator.enabled` | `bool` | `false` | Enable actuator endpoints |

---

## Auto-Detection

When `pyfly.web.adapter` is `"auto"` (the default), the framework selects the web adapter using cascading auto-configuration:

1. **FastAPIAutoConfiguration** -- `@conditional_on_class("fastapi")` + `@conditional_on_missing_bean(WebServerPort)`. If FastAPI is installed and no web adapter bean exists, register `FastAPIWebAdapter`.
2. **WebAutoConfiguration** (Starlette) -- `@conditional_on_class("starlette")` + `@conditional_on_missing_bean(WebServerPort)`. Fallback when FastAPI is not installed.

Because FastAPI depends on Starlette, installing FastAPI always makes Starlette available too. The FastAPI auto-configuration is registered with a higher-priority entry point, so it is evaluated first.

To force a specific adapter:

```yaml
pyfly:
  web:
    adapter: fastapi    # or "starlette"
```

---

## Scaffolding

The `fastapi-api` archetype generates a complete FastAPI project:

```bash
pyfly new my-api --archetype fastapi-api
```

This generates the same layered structure as the `web-api` archetype (controllers, services, repositories, models), but with FastAPI as the web adapter and the `fastapi` extra in `pyproject.toml`.

```
my-api/
├── pyproject.toml            # Depends on pyfly[fastapi]
├── pyfly.yaml                # web.adapter: auto (FastAPI auto-detected)
├── Dockerfile
├── README.md
├── .gitignore
├── .env.example
├── src/my_api/
│   ├── __init__.py
│   ├── app.py
│   ├── main.py
│   ├── controllers/
│   │   ├── __init__.py
│   │   ├── health_controller.py
│   │   └── todo_controller.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── todo_service.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── todo.py
│   └── repositories/
│       ├── __init__.py
│       └── todo_repository.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_todo_service.py
```

---

## Controller Registration

The `FastAPIControllerRegistrar` discovers `@rest_controller` beans from the DI container and registers their routes with the FastAPI application, just like the Starlette `ControllerRegistrar`. The same decorators and parameter types are used:

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping, get_mapping, post_mapping, PathVar, Valid

@rest_controller
@request_mapping("/api/orders")
class OrderController:
    def __init__(self, order_service: OrderService) -> None:
        self._service = order_service

    @get_mapping("/")
    async def list_orders(self) -> list[dict]:
        return await self._service.find_all()

    @get_mapping("/{order_id}")
    async def get_order(self, order_id: PathVar[str]) -> dict:
        return await self._service.find_by_id(order_id)

    @post_mapping("/", status_code=201)
    async def create_order(self, body: Valid[CreateOrderRequest]) -> dict:
        return await self._service.create(body)
```

This controller works identically with both the Starlette and FastAPI adapters. No code changes are required.

---

## Built-in OpenAPI

When the FastAPI adapter is active and `pyfly.web.docs.enabled` is `true`, the following endpoints are available natively through FastAPI:

- `/docs` -- Swagger UI (interactive API explorer)
- `/redoc` -- ReDoc (clean API reference)
- `/openapi.json` -- OpenAPI 3.1 specification

FastAPI generates OpenAPI schemas from its own route definitions, which means the OpenAPI spec reflects the exact routes registered by `FastAPIControllerRegistrar`. PyFly's `OpenAPIGenerator` is not used when FastAPI is active -- FastAPI handles it natively.

---

## Middleware and Filters

The FastAPI adapter reuses the same `WebFilterChainMiddleware` and built-in filters as the Starlette adapter. Always-active filters:

| Filter | Purpose |
|--------|---------|
| `RequestContextFilter` | Initializes request-scoped context (runs first) |
| `CorrelationFilter` | Stamps W3C trace context and correlation headers |
| `TransactionIdFilter` | Propagates or generates `X-Transaction-Id` |
| `RequestLoggingFilter` | Logs request method, path, status, and duration |
| `SecurityHeadersFilter` | Adds OWASP security headers |

Opt-in filters (registered as beans) such as `SecurityFilter`, `HttpSecurityFilter`, and `CsrfFilter` are auto-discovered and added to the chain when present.

Custom `WebFilter` beans registered in the DI container are auto-discovered and added to the chain, exactly as they are with the Starlette adapter.

This works because FastAPI is built on Starlette, so all Starlette middleware (including `WebFilterChainMiddleware`) is fully compatible.

---

## Migration from Starlette

Switching from the Starlette adapter to FastAPI is a drop-in change:

1. Install FastAPI: `uv add "pyfly[fastapi]"`
2. Done. No controller changes needed.

The FastAPI adapter is auto-detected and preferred over Starlette when both are installed. Your existing `@rest_controller` classes, `@get_mapping`/`@post_mapping` decorators, parameter binding types (`Body`, `PathVar`, `QueryParam`, `Header`, `Cookie`, `Valid`), exception handlers, and WebFilter implementations all work without modification.

The only visible difference is that `/docs` and `/redoc` are now served by FastAPI's native OpenAPI integration rather than PyFly's `OpenAPIGenerator`.

To explicitly control which adapter is used:

```yaml
pyfly:
  web:
    adapter: fastapi    # Force FastAPI
    # adapter: starlette  # Force Starlette
```

---

## Testing

Use FastAPI's `TestClient` or `httpx.AsyncClient` for integration tests:

```python
from fastapi.testclient import TestClient

def test_hello(app):
    client = TestClient(app)
    response = client.get("/api/hello")
    assert response.status_code == 200
```

Since FastAPI's `TestClient` is Starlette's `TestClient`, existing Starlette-based tests work without changes.

---

## See Also

- [Web Module Guide](../modules/web.md) -- Full API reference: controllers, routing, middleware, CORS, OpenAPI
- [Starlette Adapter](starlette.md) -- The Starlette peer adapter
- [Granian Adapter](granian.md) -- High-performance ASGI server
- [Server Module Guide](../modules/server.md) -- Pluggable server architecture
- [Adapter Catalog](README.md)
