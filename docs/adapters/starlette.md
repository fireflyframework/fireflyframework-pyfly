# Starlette Adapter

> **Module:** Web — [Module Guide](../modules/web.md)
> **Package:** `pyfly.web.adapters.starlette`
> **Backend:** Starlette 0.40+, Uvicorn 0.30+

## Quick Start

### Installation

```bash
uv add "pyfly[web]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  web:
    port: 8080
    host: "0.0.0.0"
```

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
```

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.web.adapter` | `str` | `"auto"` | Adapter selection (`auto` or `starlette`) |
| `pyfly.web.port` | `int` | `8000` | Server port |
| `pyfly.web.host` | `str` | `"0.0.0.0"` | Server bind address |
| `pyfly.web.debug` | `bool` | `false` | Debug mode |
| `pyfly.web.docs.enabled` | `bool` | `true` | Enable OpenAPI / Swagger UI / ReDoc |
| `pyfly.web.actuator.enabled` | `bool` | `false` | Enable actuator endpoints |

---

## Adapter-Specific Features

### StarletteWebAdapter

Implements `WebServerPort`. Creates a Starlette `Application` with routes, middleware, and exception handlers wired from the DI container.

### Controller Auto-Discovery

`ControllerRegistrar` scans the DI container for `@rest_controller` beans and registers their `@get_mapping` / `@post_mapping` / etc. routes with the Starlette router.

### Parameter Resolution

`ParameterResolver` maps controller method parameters to HTTP request data:

- `Body[T]` — JSON request body (Pydantic-validated)
- `PathVar[T]` — URL path variable
- `QueryParam[T]` — Query string parameter
- `Header[T]` — HTTP header value
- `Cookie[T]` — Cookie value

### WebFilter Chain

`WebFilterChainMiddleware` runs an ordered chain of filters on every request. Always-active built-in filters:

| Filter | Purpose |
|--------|---------|
| `RequestContextFilter` | Initializes request-scoped context (runs first) |
| `CorrelationFilter` | Stamps W3C trace context and correlation headers |
| `TransactionIdFilter` | Propagates or generates `X-Transaction-Id` |
| `RequestLoggingFilter` | Logs request method, path, status, and duration |
| `SecurityHeadersFilter` | Adds OWASP security headers |

Opt-in filters (registered as beans):

| Filter | Purpose |
|--------|---------|
| `SecurityFilter` | JWT Bearer token authentication |
| `HttpSecurityFilter` | URL-pattern authorization rules |
| `CsrfFilter` | Double-submit cookie CSRF protection |

### OpenAPI & Documentation

When `pyfly.web.docs.enabled` is `true`:

- `/openapi.json` — OpenAPI 3.1 spec (auto-generated from controllers)
- `/docs` — Swagger UI
- `/redoc` — ReDoc

### Global Exception Handler

Maps PyFly exceptions to RFC 7807 error responses automatically. No `@ControllerAdvice` needed.

### Actuator Routes

When `pyfly.web.actuator.enabled` is `true`, the adapter mounts actuator endpoints at `/actuator/*` via `make_starlette_actuator_routes()`.

---

## Testing

Use Starlette's `TestClient` or `httpx.AsyncClient` for integration tests:

```python
from starlette.testclient import TestClient

def test_hello(app):
    client = TestClient(app)
    response = client.get("/api/hello")
    assert response.status_code == 200
```

---

## See Also

- [Web Module Guide](../modules/web.md) — Full API reference: controllers, routing, middleware, CORS, OpenAPI
- [WebFilters Guide](../modules/web-filters.md) — Filter chain details
- [Actuator Guide](../modules/actuator.md) — Health checks and monitoring endpoints
- [Adapter Catalog](README.md)
