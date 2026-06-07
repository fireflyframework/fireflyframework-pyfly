<span class="eyebrow">Chapter 4</span>

# Your First HTTP API {.chtitle}

::: figure art/openers/ch04.svg | &nbsp;

Lumen has wired services, a clean configuration story, and a lifecycle that spans development through production. The only thing missing is a way for the outside world to talk to it. This chapter closes Part I by turning `WalletService` into a clean, validated REST API — one with automatic OpenAPI documentation, structured error responses that clients can trust, and the same framework-managed conventions you have come to expect from every other part of PyFly.

---

## Controllers and route mappings

A **controller** in PyFly is an ordinary Python class that the DI container manages and the web layer routes requests into. You mark it with two decorators: `@rest_controller` from `pyfly.container` (which registers it as a bean and sets its stereotype) and `@request_mapping` from `pyfly.web` (which sets the URL prefix for every handler in the class).

Route handlers are plain `async def` methods on that class, each decorated with `@get_mapping`, `@post_mapping`, `@put_mapping`, `@patch_mapping`, or `@delete_mapping`. Every mapping decorator takes an optional relative path and an optional `status_code`. The full URL is the base path from `@request_mapping` concatenated with the relative path from the method decorator.

Here is Lumen's wallet controller. It delegates everything to the `WalletService` you built in Chapter 2 and uses the basic parameter-binding types you will read about in the next section:

::: listing lumen/wallet_controller.py | Listing 4.1 — WalletController delegating to WalletService
from pydantic import BaseModel, Field

from pyfly.container import rest_controller
from pyfly.kernel.exceptions import ResourceNotFoundException
from pyfly.web import (
    Body,
    Header,
    PathVar,
    QueryParam,
    Valid,
    delete_mapping,
    exception_handler,
    get_mapping,
    patch_mapping,
    post_mapping,
    request_mapping,
)

from lumen.wallet_service import WalletService


class CreateWalletRequest(BaseModel):
    owner_id: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class DepositRequest(BaseModel):
    amount: float = Field(gt=0)


@rest_controller
@request_mapping("/wallets")
class WalletController:

    def __init__(self, wallet_service: WalletService) -> None:
        self._service = wallet_service

    @post_mapping("", status_code=201)
    async def create_wallet(
        self, body: Valid[CreateWalletRequest]
    ) -> dict:
        return await self._service.create_wallet(body.owner_id)

    @get_mapping("/{wallet_id}")
    async def get_wallet(self, wallet_id: PathVar[str]) -> dict:
        wallet = await self._service.get_wallet(wallet_id)
        if wallet is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return wallet

    @get_mapping("/{wallet_id}/balance")
    async def get_balance(self, wallet_id: PathVar[str]) -> dict:
        wallet = await self._service.get_wallet(wallet_id)
        if wallet is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return {"wallet_id": wallet_id, "balance": wallet["balance"]}

    @patch_mapping("/{wallet_id}/deposit")
    async def deposit(
        self, wallet_id: PathVar[str], body: Valid[DepositRequest]
    ) -> dict:
        result = await self._service.credit(wallet_id, body.amount)
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("")
    async def list_wallets(
        self,
        owner_id: QueryParam[str] = None,
        page: QueryParam[int] = 1,
        size: QueryParam[int] = 20,
    ) -> list:
        return await self._service.find_wallets(
            owner_id=owner_id, page=page, size=size
        )

    @exception_handler(ResourceNotFoundException)
    async def handle_not_found(self, exc: ResourceNotFoundException):
        return 404, {
            "error": {
                "message": str(exc),
                "code": exc.code,
                "context": exc.context,
            }
        }
:::

The `@rest_controller` decorator does two things simultaneously: it registers `WalletController` as a singleton bean in the DI container, and it sets the `__pyfly_stereotype__` marker that the `ControllerRegistrar` uses to discover and register routes at startup. Constructor injection wires `WalletService` automatically from the type hint — no `@Autowired`, no factory, no configuration file.

::: figure art/figures/04-request.svg | Figure 4.1 — How a request flows to your handler.

The five mapping decorators accept the same two parameters:

| Parameter | Default | Description |
|---|---|---|
| `path` | `""` | Relative path appended to the base. Use `{name}` for path variables. |
| `status_code` | `200` | HTTP status code for a successful response. |

`@post_mapping("", status_code=201)` maps `POST /wallets` and returns 201 on success. `@get_mapping("/{wallet_id}")` maps `GET /wallets/{wallet_id}`. The paths are concatenated at startup; duplicate or trailing slashes are normalised automatically.

!!! spring "Spring parity"
    `@rest_controller` + `@request_mapping` + `@get_mapping` / `@post_mapping` is a direct translation of Spring's `@RestController` + `@RequestMapping` + `@GetMapping` / `@PostMapping`. Handler methods return values directly (not `ResponseEntity`) and the framework converts them to JSON — exactly the pattern Spring encourages with `@ResponseBody` on `@RestController`.

---

## Binding request data

PyFly uses **generic type annotations** to declare where a handler parameter comes from. The `ParameterResolver` inspects each handler signature at startup and builds a resolution plan so there is zero overhead per request for introspection. Five binding types cover every part of an HTTP request:

### PathVar[T] — path variables

Extracts a segment from the URL path. The parameter name must match a `{placeholder}` in the route.

```python
@get_mapping("/{wallet_id}")
async def get_wallet(self, wallet_id: PathVar[str]) -> dict:
    ...

@get_mapping("/{wallet_id}/transactions/{txn_id}")
async def get_transaction(
    self,
    wallet_id: PathVar[str],
    txn_id: PathVar[str],
) -> dict:
    ...
```

`PathVar` coerces the raw string segment to `T` automatically. `PathVar[int]`, `PathVar[float]`, and `PathVar[UUID]` all work; the coercion calls `int(value)`, `float(value)`, and `UUID(value)` respectively.

### QueryParam[T] — query parameters

Extracts a value from the query string. Supports defaults and optional values.

```python
@get_mapping("")
async def list_wallets(
    self,
    owner_id: QueryParam[str] = None,
    page: QueryParam[int] = 1,
    size: QueryParam[int] = 20,
) -> list:
    ...
```

A parameter is **required** when it has no Python default and its type does not admit `None`. Missing a required `QueryParam` raises `InvalidRequestException` (HTTP 400). To make a parameter optional, either give it a default value or annotate it `QueryParam[str | None]`.

### Body[T] — request body

Deserialises the JSON (or XML) request body. When `T` is a Pydantic `BaseModel`, `model_validate_json()` is called automatically.

```python
@post_mapping("", status_code=201)
async def create_wallet(self, body: Body[CreateWalletRequest]) -> dict:
    return await self._service.create_wallet(body.owner_id)
```

### Header[T] and Cookie[T]

Extract values from request headers and cookies. The parameter name is converted from `snake_case` to `kebab-case` for headers:

```python
@get_mapping("/me")
async def get_my_wallets(
    self,
    x_api_key: Header[str],
    session_id: Cookie[str | None],
) -> list:
    ...
```

`x_api_key: Header[str]` reads the `x-api-key` header. A missing required header or cookie raises `InvalidRequestException` (HTTP 400), just like a missing query parameter.

!!! tip "Tip"
    All five binding types follow the same **required vs optional** rule: no default + non-`None` type = required (HTTP 400 when absent); any default or `T | None` = optional. The rule is uniform across `QueryParam`, `Header`, and `Cookie` — you learn it once, it applies everywhere.

---

## Validation with Valid[T]

Pydantic `BaseModel` gives you field-level constraints for free. `Valid[T]` is PyFly's marker type that ensures those constraints produce a **structured 422 response** instead of a raw Pydantic `ValidationError` bubbling up to a 500.

### Pydantic DTOs for Lumen

Here are the two request models used in `WalletController`:

::: listing lumen/wallet_dtos.py | Listing 4.2 — Pydantic request models for the wallet API
from pydantic import BaseModel, Field


class CreateWalletRequest(BaseModel):
    owner_id: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class DepositRequest(BaseModel):
    amount: float = Field(gt=0)
:::

`Field(gt=0)` means the value must be greater than zero. `Field(min_length=1)` prevents empty strings. These constraints are standard Pydantic — PyFly adds nothing special to the models themselves.

### Using Valid[T] in a handler

Wrap the binding type in `Valid` to opt into structured 422 errors:

```python
@post_mapping("", status_code=201)
async def create_wallet(self, body: Valid[CreateWalletRequest]) -> dict:
    return await self._service.create_wallet(body.owner_id)

@patch_mapping("/{wallet_id}/deposit")
async def deposit(
    self, wallet_id: PathVar[str], body: Valid[DepositRequest]
) -> dict:
    ...
```

`Valid[CreateWalletRequest]` is shorthand for `Valid[Body[CreateWalletRequest]]` — it implies body binding. When the request body fails Pydantic validation, the resolver catches the `ValidationError` and raises a `ValidationException` with `code="VALIDATION_ERROR"` and a `context.errors` array containing each field-level error.

### What the client sees on failure

Send a `POST /wallets` with an empty `owner_id` and a negative amount on a different request:

```
POST /wallets
Content-Type: application/json

{"owner_id": ""}
```

The response is HTTP 422:

```json
{
  "error": {
    "message": "Validation failed: owner_id: String should have at least 1 character",
    "code": "VALIDATION_ERROR",
    "status": 422,
    "path": "/wallets",
    "timestamp": "2026-06-07T10:30:00+00:00",
    "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "context": {
      "errors": [
        {
          "type": "string_too_short",
          "loc": ["owner_id"],
          "msg": "String should have at least 1 character",
          "input": "",
          "ctx": {"min_length": 1}
        }
      ]
    }
  }
}
```

Every field error has a `type` (machine-readable), a `loc` (the path to the failing field), a `msg` (human-readable), and an `input` (the value that was rejected). API consumers can parse this array deterministically — no scraping error strings.

The difference between bare `Body[T]` and `Valid[T]` is precisely this:

| Annotation | On validation failure |
|---|---|
| `Body[T]` | Raw Pydantic `ValidationError` propagates — may become a 500 without extra handling |
| `Valid[T]` | Caught, converted to `ValidationException`, always produces a structured 422 |

Use `Valid[T]` for every endpoint that accepts user input.

!!! spring "Spring parity"
    `Valid[T]` maps directly to Spring's `@Valid` + `@RequestBody` combination on a `@RestController` method. In Spring you write `@PostMapping public ResponseEntity create(@Valid @RequestBody CreateWalletRequest body)`; in PyFly you write `async def create_wallet(self, body: Valid[CreateWalletRequest])`. The 422 response shape (field-level errors with location paths) mirrors Spring Boot 3's `MethodArgumentNotValidException` payload.

---

## Errors that clients can trust

PyFly's exception hierarchy is the backbone of its error story. Every exception in the tree carries three things: a human-readable `message`, a machine-readable `code`, and an optional `context` dict for debugging detail. The web layer's global exception handler maps each subclass to the correct HTTP status code automatically — you `raise`, the framework responds.

### The exception tree

```
PyFlyException
├── BusinessException          → 400 (catch-all)
│   ├── ValidationException    → 422
│   ├── ResourceNotFoundException → 404
│   ├── ConflictException      → 409
│   ├── InvalidRequestException → 400
│   └── ...
├── SecurityException          → 403
│   ├── UnauthorizedException  → 401
│   └── ForbiddenException     → 403
└── InfrastructureException    → 502 (catch-all)
    ├── ServiceUnavailableException → 503
    ├── CircuitBreakerException → 503
    └── ...
```

Import them from `pyfly.kernel.exceptions`:

```python
from pyfly.kernel.exceptions import (
    ResourceNotFoundException,
    ConflictException,
    ValidationException,
    InvalidRequestException,
)
```

Raise them from service or controller code without worrying about HTTP:

```python
raise ResourceNotFoundException(
    f"Wallet {wallet_id} not found",
    code="WALLET_NOT_FOUND",
    context={"wallet_id": wallet_id},
)
```

The global handler catches it, maps it to 404, and emits a structured JSON response:

```json
{
  "error": {
    "message": "Wallet w-999 not found",
    "code": "WALLET_NOT_FOUND",
    "status": 404,
    "path": "/wallets/w-999",
    "timestamp": "2026-06-07T10:30:00+00:00",
    "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "context": {
      "wallet_id": "w-999"
    }
  }
}
```

You get the `transaction_id` for free — the `TransactionIdFilter` assigns a UUID to every request and threads it through to all error responses. Clients can log it and correlate it with your structured server logs.

### Controller-level @exception_handler

When you want to customise the response shape for a specific exception type — or handle a domain error that only makes sense inside one controller — decorate a method with `@exception_handler(ExceptionType)`:

::: listing lumen/wallet_controller_handler.py | Listing 4.3 — Controller-scoped exception handler
from pyfly.kernel.exceptions import ResourceNotFoundException
from pyfly.web import exception_handler


class WalletNotFound(ResourceNotFoundException):
    """Domain-specific not-found error for wallets."""
    pass


async def handle_wallet_not_found(
    self, exc: WalletNotFound
) -> tuple:
    return 404, {
        "error": {
            "message": str(exc),
            "code": exc.code,
            "context": exc.context,
        }
    }

handle_wallet_not_found = exception_handler(WalletNotFound)(
    handle_wallet_not_found
)
:::

More naturally, the `@exception_handler` decorator lives directly on the method inside the controller class (as shown in Listing 4.1). When multiple handlers could match, the most-specific subclass wins. Returning a `(status_code, body)` tuple is the most concise form; you can also return a Starlette `Response` directly for full control.

!!! note "RFC 7807"
    The default error envelope — `{"error": {...}}` — is PyFly's own format. If your team prefers the IETF standard, set `pyfly.web.problem-details.enabled: true` in `pyfly.yaml`. With that flag on, the same `ResourceNotFoundException` produces an `application/problem+json` response with `type`, `title`, `status`, `detail`, and `instance` as the standard RFC 7807 members, plus `code` and `transactionId` as PyFly extension members. Both modes use the same exception hierarchy and status mapping.

---

## Content negotiation & OpenAPI

### JSON and XML

PyFly's response pipeline runs through an ordered `HttpMessageConverter` chain. JSON is the default — when no `Accept` header is sent, the response is `application/json`. If the client sends `Accept: application/xml`, the XML converter takes over and serialises the same return value as XML, with no changes to your handler code:

```
GET /wallets/w-001   Accept: application/json  →  {"id": "w-001", ...}
GET /wallets/w-001   Accept: application/xml   →  <response><id>w-001</id>...</response>
```

The same negotiation applies on reads: a `Body[T]` or `Valid[T]` parameter accepts both `Content-Type: application/json` and `Content-Type: application/xml` request bodies. JSON is the fallback when no `Content-Type` is present.

### Auto-generated documentation

As soon as Lumen starts, three documentation endpoints are live at no cost:

| Endpoint | Purpose |
|---|---|
| `/docs` | Swagger UI — interactive, try-it-now documentation |
| `/redoc` | ReDoc — clean, two-panel reference documentation |
| `/openapi.json` | Raw OpenAPI 3.0 specification |

The `OpenAPIGenerator` introspects `ControllerRegistrar`'s route metadata — every path, method, path variable, query parameter, and request body schema (from Pydantic model introspection) — and assembles the spec at startup. You never write the spec by hand. Disable it in production by setting `pyfly.web.docs.enabled: false` in `pyfly.yaml`.

!!! tip "Tip"
    Open `http://localhost:8080/docs` while Lumen is running. You will see `POST /wallets`, `GET /wallets/{wallet_id}`, `PATCH /wallets/{wallet_id}/deposit`, and the others — each with the correct request schema derived from your Pydantic model, and the query parameters from `list_wallets` already documented with their types and defaults.

---

## The server underneath

PyFly does not hardcode an ASGI server. At startup, `ServerAutoConfiguration` runs a cascading selection based on what is installed:

| Priority | Server | Characteristic |
|---|---|---|
| 1st | **Granian** | Rust/tokio-powered; fastest single-worker throughput |
| 2nd | **Uvicorn** | Ecosystem standard; best tooling support |
| 3rd | **Hypercorn** | Native HTTP/2 and HTTP/3 |

All three are started through the same `ApplicationServerPort` protocol, so your code is unaware of which one runs. Override with `pyfly.server.type: uvicorn` in `pyfly.yaml` or with the `--server` CLI flag:

```bash
pyfly run --server uvicorn --reload      # development: auto-reload
pyfly run --server granian --workers 4  # production: multi-worker
```

The event loop is also pluggable: `uvloop` (Linux/macOS) and `winloop` (Windows) are selected automatically when installed, giving a 2–4× throughput improvement over the asyncio default. Install them with `uv add "pyfly[web-fast]"`.

!!! tip "Tip"
    For development, `pyfly run --reload` is all you need — it picks the best available server and event loop automatically. For production, `pyfly run --server granian --workers 0` resolves `0` to the CPU count, maximising throughput. CLI flags always override `pyfly.yaml`.

---

## What you built {.recap}

Part I is complete.

In four chapters you went from an empty scaffold to a production-shaped service. Lumen now **boots** (`@pyfly_application`, startup banner, structured logging), is **wired** (`WalletService`, `WalletRepository`, `EventPublisher` connected through constructor injection with no glue code), **configured** (four-layer `pyfly.yaml` + profile overlays + env-var secrets, typed `WalletProperties`), and **serves** — a validated REST API at `/wallets` with `PathVar`, `QueryParam`, and `Valid[T]` body binding, structured 422 errors from Pydantic constraints, domain-error-to-status mapping from the exception hierarchy, auto-generated OpenAPI documentation at `/docs`, and a pluggable ASGI server running underneath.

Every part of this stack follows the same hexagonal principle you have seen throughout: your code depends on ports and decorators, the framework wires the adapters. You can swap the in-memory repository for a PostgreSQL adapter, replace Granian with Uvicorn, or enable XML responses — none of it requires touching `WalletService` or `WalletController`.

Part II will take Lumen further: persistent data with `R2DBC` and `SQLAlchemy`, domain events with Kafka, resilience with circuit breakers, and security with JWT. The foundations you laid here carry forward intact.

---

## Try it yourself {.exercises}

1. **Add a `DELETE /wallets/{wallet_id}` endpoint.** Add a `delete_wallet` method to `WalletService` that removes the wallet from the repository (or raises `ResourceNotFoundException` if it does not exist). Wire it to `WalletController` with `@delete_mapping("/{wallet_id}", status_code=204)`. Return `None` from the handler — PyFly converts a `None` return with the default 200 status into a 204 No Content response, but when you pass `status_code=204` explicitly the mapping holds regardless. Verify with `curl -X DELETE http://localhost:8080/wallets/{id}`.

2. **Add a query filter with `QueryParam`.** Extend `list_wallets` with a `currency: QueryParam[str] = None` parameter and filter the results in the service layer when it is not `None`. Test with `GET /wallets?currency=EUR` and confirm only matching wallets are returned; confirm `GET /wallets` without the parameter returns all wallets. Then make the parameter required by removing the default and the `None` type — observe the 400 response when you omit it from the request.

3. **Add an `@exception_handler` for a new domain error.** Create a `WalletFrozenError` that subclasses `BusinessException` and raise it from a new `freeze_wallet` method in `WalletService`. Add a `@post_mapping("/{wallet_id}/freeze", status_code=200)` handler to `WalletController`, and an `@exception_handler(WalletFrozenError)` method that returns a 409 response with a clear message. Verify that hitting the endpoint a second time after freezing returns the structured 409, while other endpoints still return their usual shapes.
