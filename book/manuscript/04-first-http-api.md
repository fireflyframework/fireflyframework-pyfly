<span class="eyebrow">Chapter 4</span>

# Your First HTTP API {.chtitle}

::: figure art/openers/ch04.svg | &nbsp;

Lumen has wired services, a clean configuration story, and a lifecycle that
spans development through production. The only thing missing is a way for the
outside world to talk to it. This chapter closes Part I by turning the wallet
domain into a clean, validated REST API — one with automatic OpenAPI
documentation, structured error responses that clients can trust, and the same
framework-managed conventions you have come to expect from every other part of
PyFly.

---

## Controllers and route mappings

Every web framework needs to answer two questions: how does a request find the
right function to handle it, and how does that function get the dependencies it
needs to do its job? Frameworks that answer these questions inconsistently force
you to maintain separate wiring for the HTTP layer — a router file in one place,
dependency injection glue in another, and documentation scaffolding somewhere
else entirely. PyFly collapses all three concerns into a single class.

A **controller** in PyFly is an ordinary Python class that the DI container
manages and the web layer routes requests into. You mark it with two decorators:
`@rest_controller` from `pyfly.container` (which registers it as a bean and sets
its stereotype) and `@request_mapping` from `pyfly.web` (which sets the URL
prefix for every handler in the class).

Route handlers are plain `async def` methods on that class, each decorated with
`@get_mapping`, `@post_mapping`, `@put_mapping`, `@patch_mapping`, or
`@delete_mapping`. Every mapping decorator takes an optional relative path and an
optional `status_code`. The full URL is the base path from `@request_mapping`
concatenated with the relative path from the method decorator.

### A pre-CQRS reading example

Chapter 7 introduces the full CQRS command/query bus that Lumen uses in
production. Here in Part I we teach the web-layer mechanics on the same wallet
domain — but backed by a simple in-memory store rather than the bus. The
controller structure, imports, and decorator shapes are *identical* to what you
will see in Chapter 7; only the dispatch target changes.

::: listing lumen/web/controllers/wallet_controller.py | Listing 4.1 — WalletController using real PyFly web decorators
from __future__ import annotations

from pyfly.container import rest_controller
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import (
    Body,
    PathVar,
    QueryParam,
    Valid,
    get_mapping,
    post_mapping,
    request_mapping,
)

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto


# ---------------------------------------------------------------------------
# In-memory store (replaced by a database repository in Chapter 5)
# ---------------------------------------------------------------------------
_wallets: dict[str, WalletDto] = {}


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, inspect.

    In Part I the controller holds a minimal in-memory store so you can
    focus on the web-layer mechanics — decorators, binding, validation, and
    error handling — without persistence or CQRS machinery. Chapter 7
    replaces the store with DefaultCommandBus / DefaultQueryBus dispatching.
    """

    @post_mapping("", status_code=201)
    async def open_wallet(
        self, request: Valid[Body[OpenWalletRequest]]
    ) -> dict[str, str]:
        import uuid
        wallet_id = str(uuid.uuid4())
        wallet = WalletDto(
            id=wallet_id,
            owner_id=request.owner_id,
            currency=request.currency,
            balance_minor=0,
            balance=0.0,
            created_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
        )
        _wallets[wallet_id] = wallet
        return {"wallet_id": wallet_id}

    @get_mapping("/{wallet_id}")
    async def get_wallet(self, wallet_id: PathVar[str]) -> WalletDto:
        result = _wallets.get(wallet_id)
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("/{wallet_id}/balance")
    async def get_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        wallet = _wallets.get(wallet_id)
        if wallet is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return BalanceDto(
            id=wallet.id,
            currency=wallet.currency,
            balance_minor=wallet.balance_minor,
            balance=wallet.balance,
        )

    @post_mapping("/{wallet_id}/deposit")
    async def deposit(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
    ) -> dict[str, int | str]:
        wallet = _wallets.get(wallet_id)
        if wallet is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        new_balance = wallet.balance_minor + request.amount
        _wallets[wallet_id] = wallet.model_copy(
            update={
                "balance_minor": new_balance,
                "balance": new_balance / 100,
            }
        )
        return {"wallet_id": wallet_id, "balance_minor": new_balance}

    @get_mapping("")
    async def list_wallets(
        self,
        owner_id: QueryParam[str] = None,
    ) -> list[WalletDto]:
        wallets = list(_wallets.values())
        if owner_id is not None:
            wallets = [w for w in wallets if w.owner_id == owner_id]
        return wallets
:::

Let's walk through the key design choices in this listing before moving on.

`@rest_controller` does two things simultaneously: it registers `WalletController`
as a singleton bean in the DI container, and it sets the `__pyfly_stereotype__`
marker that the `ControllerRegistrar` uses to discover and register routes at
startup. The pairing with `@request_mapping("/api/v1/wallets")` means every
method-level decorator inherits `/api/v1/wallets` as its prefix — you write the
base path once and never repeat it.

Notice there is no `__init__` with an injected collaborator in this version. The
in-memory `_wallets` dictionary is a module-level store that suffices for Part I.
Chapter 5 introduces repositories, and Chapter 7 shows the production pattern:
a constructor that takes `DefaultCommandBus` and `DefaultQueryBus` injected by
the DI container, with each handler dispatching a command or query through the
bus instead of reading `_wallets` directly.

Each handler returns a Pydantic model (`WalletDto`, `BalanceDto`) or a plain
`dict`. The framework serialises the return value to JSON and sets the
`Content-Type` header — the handler never builds a response object. The
`status_code=201` argument to `@post_mapping` tells the framework to use 201
Created for successful wallet opening; all other handlers default to 200.

The five mapping decorators accept the same two parameters:

| Parameter | Default | Description |
|---|---|---|
| `path` | `""` | Relative path appended to the base. Use `{name}` for path variables. |
| `status_code` | `200` | HTTP status code for a successful response. |

`@post_mapping("", status_code=201)` maps `POST /api/v1/wallets` and returns 201
on success. `@get_mapping("/{wallet_id}")` maps `GET /api/v1/wallets/{wallet_id}`.
The paths are concatenated at startup; duplicate or trailing slashes are
normalised automatically.

::: figure art/figures/04-request.svg | Figure 4.1 — How a request flows to your handler.

!!! spring "Spring parity"
    `@rest_controller` + `@request_mapping` + `@get_mapping` / `@post_mapping`
    is a direct translation of Spring's `@RestController` + `@RequestMapping` +
    `@GetMapping` / `@PostMapping`. Handler methods return values directly (not
    `ResponseEntity`) and the framework converts them to JSON — exactly the
    pattern Spring encourages with `@ResponseBody` on `@RestController`.

---

## Binding request data

A request carries data in several places at once: a segment of the URL path
identifies the resource, the query string carries filters and pagination, the
body carries the payload, and headers carry metadata. Most frameworks handle
these through separate mechanisms that each have their own conventions to learn.
PyFly unifies them under a single idea: **generic type annotations on handler
parameters declare where data comes from**.

PyFly uses this approach because handler signatures become self-documenting.
Looking at the parameter list of any handler tells you exactly which parts of
the request it reads, and what types it expects them to be, without opening a
separate router file or reading framework documentation. The `ParameterResolver`
inspects each handler signature at startup and builds a resolution plan so
there is zero overhead per request for introspection. Five binding types cover
every part of an HTTP request:

### PathVar[T] — path variables

Extracts a segment from the URL path. The parameter name must match a
`{placeholder}` in the route.

```python
@get_mapping("/{wallet_id}")
async def get_wallet(self, wallet_id: PathVar[str]) -> WalletDto:
    ...

@get_mapping("/{wallet_id}/transactions/{txn_id}")
async def get_transaction(
    self,
    wallet_id: PathVar[str],
    txn_id: PathVar[str],
) -> dict:
    ...
```

`PathVar` coerces the raw string segment to `T` automatically. `PathVar[int]`,
`PathVar[float]`, and `PathVar[UUID]` all work; the coercion calls `int(value)`,
`float(value)`, and `UUID(value)` respectively.

### QueryParam[T] — query parameters

Extracts a value from the query string. Supports defaults and optional values.

```python
@get_mapping("")
async def list_wallets(
    self,
    owner_id: QueryParam[str] = None,
    page: QueryParam[int] = 1,
    size: QueryParam[int] = 20,
) -> list[WalletDto]:
    ...
```

A parameter is **required** when it has no Python default and its type does not
admit `None`. Missing a required `QueryParam` raises `InvalidRequestException`
(HTTP 400). To make a parameter optional, either give it a default value or
annotate it `QueryParam[str | None]`.

### Body[T] — request body

Deserialises the JSON (or XML) request body. When `T` is a Pydantic `BaseModel`,
`model_validate_json()` is called automatically.

```python
@post_mapping("", status_code=201)
async def open_wallet(
    self, request: Valid[Body[OpenWalletRequest]]
) -> dict[str, str]:
    ...
```

### Header[T] and Cookie[T]

Extract values from request headers and cookies. The parameter name is converted
from `snake_case` to `kebab-case` for headers:

```python
@get_mapping("/me")
async def get_my_wallets(
    self,
    x_api_key: Header[str],
    session_id: Cookie[str | None],
) -> list[WalletDto]:
    ...
```

`x_api_key: Header[str]` reads the `x-api-key` header. A missing required
header or cookie raises `InvalidRequestException` (HTTP 400), just like a
missing query parameter.

!!! tip "Tip"
    All five binding types follow the same **required vs optional** rule: no
    default + non-`None` type = required (HTTP 400 when absent); any default or
    `T | None` = optional. The rule is uniform across `QueryParam`, `Header`,
    and `Cookie` — you learn it once, it applies everywhere.

---

## Validation with Valid[T]

Binding tells the framework where data comes from. Validation tells it what that
data must look like before your handler ever sees it. Without a layer that
intercepts bad input early, validation logic ends up scattered across service
methods, manual `if` blocks appear throughout business code, and different
handlers produce inconsistent error responses depending on where they happen to
catch the problem.

PyFly solves this cleanly. Pydantic `BaseModel` gives you field-level constraints
for free. `Valid[T]` is PyFly's marker type that ensures those constraints produce
a **structured 422 response** instead of a raw Pydantic `ValidationError`
bubbling up to a 500.

### Pydantic DTOs for Lumen

Here are the request and response DTOs used in Lumen's wallet API. The real
files live under `lumen/interfaces/dtos/v1/` — one file per DTO.

::: listing lumen/interfaces/dtos/v1/open_wallet_request.py | Listing 4.2a — OpenWalletRequest: wallet-opening payload
from __future__ import annotations

from pydantic import BaseModel, Field

from lumen.interfaces.enums.v1.currency import Currency


class OpenWalletRequest(BaseModel):
    """Wallet-opening request payload."""

    owner_id: str = Field(
        min_length=1,
        max_length=64,
        description="Identifier of the wallet owner",
    )
    currency: Currency = Field(
        default=Currency.EUR,
        description="ISO-4217 currency the wallet holds",
    )
:::

::: listing lumen/interfaces/dtos/v1/deposit_request.py | Listing 4.2b — DepositRequest: deposit/withdrawal payload
from __future__ import annotations

from pydantic import BaseModel, Field


class DepositRequest(BaseModel):
    """Deposit/withdrawal request payload.

    Shared by POST /{id}/deposit and POST /{id}/withdraw — both move a
    positive amount of money in the wallet's own currency.
    """

    amount: int = Field(
        gt=0,
        description="Amount in minor units (cents); must be positive",
    )
:::

::: listing lumen/interfaces/dtos/v1/wallet_dto.py | Listing 4.2c — WalletDto: full wallet response
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency


class WalletDto(BaseModel):
    """Full wallet representation returned to clients.

    ``balance_minor`` is in minor units (cents); ``balance`` is the same
    value rendered as a major-unit decimal for human-friendly display.
    """

    id: str
    owner_id: str
    currency: Currency
    balance_minor: int
    balance: float
    created_at: datetime
:::

::: listing lumen/interfaces/dtos/v1/balance_dto.py | Listing 4.2d — BalanceDto: lightweight balance projection
from __future__ import annotations

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency


class BalanceDto(BaseModel):
    """Lightweight balance projection for the balance endpoint."""

    id: str
    currency: Currency
    balance_minor: int
    balance: float
:::

These are pure Pydantic models — PyFly adds nothing to them. Let's unpack the
key decisions.

`OpenWalletRequest.owner_id` uses `Field(min_length=1, max_length=64)`. The
lower bound prevents phantom wallets from empty-string owner IDs that would
silently pollute your data. The upper bound keeps identifiers within a
reasonable column width when you add the database layer in Chapter 5.

`currency` is a `Currency` enum (a `StrEnum` with `EUR`, `USD`, `GBP`). Using
an enum rather than a raw string means Pydantic rejects `"XYZ"` at deserialisation
time — you never have to validate the currency code in your own code.
`Field(default=Currency.EUR)` provides a sensible default so callers can omit
the field for EUR wallets.

`DepositRequest.amount` uses `int` (not `float`) with `Field(gt=0)`. Money in
minor units avoids floating-point rounding errors: `1050` means €10.50 for an
EUR wallet. The `gt=0` constraint makes a zero or negative deposit a 422 client
error rather than a business-logic decision — the constraint is in the type, and
Pydantic enforces it before your handler runs.

`WalletDto` and `BalanceDto` are response models. Returning a typed Pydantic
model from a handler (instead of a plain `dict`) lets the framework generate
accurate OpenAPI response schemas and gives clients a machine-readable contract.

### Using Valid[T] in a handler

Wrap `Body[T]` in `Valid` to opt into structured 422 errors:

```python
@post_mapping("", status_code=201)
async def open_wallet(
    self, request: Valid[Body[OpenWalletRequest]]
) -> dict[str, str]:
    ...

@post_mapping("/{wallet_id}/deposit")
async def deposit(
    self,
    wallet_id: PathVar[str],
    request: Valid[Body[DepositRequest]],
) -> dict[str, int | str]:
    ...
```

`Valid[Body[OpenWalletRequest]]` tells the resolver two things: bind from the
request body (`Body`), and run Pydantic validation before the handler executes
(`Valid`). When the body fails validation, the resolver catches the
`ValidationError` and raises a `ValidationException` with
`code="VALIDATION_ERROR"` and a `context.errors` array containing each
field-level error.

### What the client sees on failure

Send a `POST /api/v1/wallets` with an empty `owner_id`:

```
POST /api/v1/wallets
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
    "path": "/api/v1/wallets",
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

Every field error has a `type` (machine-readable), a `loc` (the path to the
failing field), a `msg` (human-readable), and an `input` (the value that was
rejected). API consumers can parse this array deterministically — no scraping
error strings.

The difference between bare `Body[T]` and `Valid[Body[T]]` is precisely this:

| Annotation | On validation failure |
|---|---|
| `Body[T]` | Raw Pydantic `ValidationError` propagates — may become a 500 without extra handling |
| `Valid[Body[T]]` | Caught, converted to `ValidationException`, always produces a structured 422 |

Use `Valid[Body[T]]` for every endpoint that accepts user input.

!!! spring "Spring parity"
    `Valid[Body[T]]` maps directly to Spring's `@Valid` + `@RequestBody`
    combination on a `@RestController` method. In Spring you write
    `@PostMapping public ResponseEntity create(@Valid @RequestBody OpenWalletRequest body)`;
    in PyFly you write
    `async def open_wallet(self, request: Valid[Body[OpenWalletRequest]])`. The
    422 response shape (field-level errors with location paths) mirrors Spring
    Boot 3's `MethodArgumentNotValidException` payload.

---

## Errors that clients can trust

A well-designed API fails loudly, consistently, and informatively. Clients
should never need to parse exception stack traces or guess what went wrong from
a generic 500. The challenge is achieving this without littering your service
code with HTTP-specific logic — the HTTP status code is an infrastructure
concern, not a business one.

PyFly's exception hierarchy is the backbone of its error story. Every exception
in the tree carries three things: a human-readable `message`, a machine-readable
`code`, and an optional `context` dict for debugging detail. The web layer's
global exception handler maps each subclass to the correct HTTP status code
automatically — you `raise`, the framework responds.

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

The hierarchy is intentionally shallow. `BusinessException` covers anything
that is the caller's fault; `InfrastructureException` covers anything that is
the system's fault. Subclasses pin the status code. When a new domain error
does not fit an existing subclass, you extend the nearest parent and the status
code comes for free.

Import them from `pyfly.kernel`:

```python
from pyfly.kernel import (
    ResourceNotFoundException,
    ConflictException,
    ValidationException,
    InvalidRequestException,
)
```

Raise them from handler code without worrying about HTTP:

```python
raise ResourceNotFoundException(
    f"Wallet {wallet_id!r} not found",
    code="WALLET_NOT_FOUND",
    context={"wallet_id": wallet_id},
)
```

The global handler catches it, maps it to 404, and emits a structured JSON
response:

```json
{
  "error": {
    "message": "Wallet 'w-999' not found",
    "code": "WALLET_NOT_FOUND",
    "status": 404,
    "path": "/api/v1/wallets/w-999",
    "timestamp": "2026-06-07T10:30:00+00:00",
    "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "context": {
      "wallet_id": "w-999"
    }
  }
}
```

You get the `transaction_id` for free — the `TransactionIdFilter` assigns a
UUID to every request and threads it through to all error responses. Clients can
log it and correlate it with your structured server logs. When a user reports an
error, a single ID is all support needs to reconstruct exactly what happened.

!!! note "RFC 7807"
    The default error envelope — `{"error": {...}}` — is PyFly's own format. If
    your team prefers the IETF standard, set
    `pyfly.web.problem-details.enabled: true` in `pyfly.yaml`. With that flag
    on, the same `ResourceNotFoundException` produces an
    `application/problem+json` response with `type`, `title`, `status`,
    `detail`, and `instance` as the standard RFC 7807 members, plus `code` and
    `transactionId` as PyFly extension members. Both modes use the same
    exception hierarchy and status mapping.

---

## Content negotiation & OpenAPI

### JSON and XML

Returning a `dict` or a Pydantic model from a handler is not quite the end of
the story. Somewhere between your handler's `return` statement and the bytes the
client receives, the framework must decide on a wire format. Rather than
hardcoding JSON, PyFly runs the return value through an ordered
`HttpMessageConverter` chain. This matters for enterprise APIs that integrate
with partners who still consume XML, or mobile clients that negotiate the
lightest format available.

JSON is the default — when no `Accept` header is sent, the response is
`application/json`. If the client sends `Accept: application/xml`, the XML
converter takes over and serialises the same return value as XML, with no
changes to your handler code:

```
GET /api/v1/wallets/w-001   Accept: application/json
  →  {"id": "w-001", ...}

GET /api/v1/wallets/w-001   Accept: application/xml
  →  <response><id>w-001</id>...</response>
```

The same negotiation applies on reads: a `Body[T]` or `Valid[Body[T]]`
parameter accepts both `Content-Type: application/json` and
`Content-Type: application/xml` request bodies. JSON is the fallback when no
`Content-Type` is present.

### Auto-generated documentation

Documentation that is written by hand drifts. As routes change, parameters are
renamed, and new models are added, manually maintained specs fall behind the
code. PyFly eliminates this entirely by generating documentation from the same
metadata that drives routing.

As soon as Lumen starts, three documentation endpoints are live at no cost:

| Endpoint | Purpose |
|---|---|
| `/docs` | Swagger UI — interactive, try-it-now documentation |
| `/redoc` | ReDoc — clean, two-panel reference documentation |
| `/openapi.json` | Raw OpenAPI 3.0 specification |

The `OpenAPIGenerator` introspects `ControllerRegistrar`'s route metadata —
every path, method, path variable, query parameter, and request/response body
schema (from Pydantic model introspection) — and assembles the spec at startup.
You never write the spec by hand. Disable it in production by setting
`pyfly.web.docs.enabled: false` in `pyfly.yaml`.

!!! tip "Tip"
    Open `http://localhost:8080/docs` while Lumen is running. You will see
    `POST /api/v1/wallets`, `GET /api/v1/wallets/{wallet_id}`,
    `POST /api/v1/wallets/{wallet_id}/deposit`, and the others — each with the
    correct request and response schemas derived from your Pydantic models, and
    the `owner_id` query parameter on `list_wallets` already documented with
    its type and default.

---

## The server underneath

At this point Lumen has routes, bindings, validation, and documentation. The
last question is: what actually listens on port 8080? The answer matters because
different servers make different trade-offs — throughput, HTTP version support,
operating system compatibility, and ecosystem tooling all vary. Locking your
application to a single server at the framework level forces you to accept those
trade-offs permanently.

PyFly does not hardcode an ASGI server. At startup, `ServerAutoConfiguration`
runs a cascading selection based on what is installed:

| Priority | Server | Characteristic |
|---|---|---|
| 1st | **Granian** | Rust/tokio-powered; fastest single-worker throughput |
| 2nd | **Uvicorn** | Ecosystem standard; best tooling support |
| 3rd | **Hypercorn** | Native HTTP/2 and HTTP/3 |

All three are started through the same `ApplicationServerPort` protocol, so
your code is unaware of which one runs. Override with
`pyfly.server.type: uvicorn` in `pyfly.yaml` or with the `--server` CLI flag:

```bash
pyfly run --server uvicorn --reload      # development: auto-reload
pyfly run --server granian --workers 4  # production: multi-worker
```

The event loop is also pluggable: `uvloop` (Linux/macOS) and `winloop`
(Windows) are selected automatically when installed, giving a 2–4× throughput
improvement over the asyncio default. Install them with
`uv add "pyfly[web-fast]"`.

!!! tip "Tip"
    For development, `pyfly run --reload` is all you need — it picks the best
    available server and event loop automatically. For production,
    `pyfly run --server granian --workers 0` resolves `0` to the CPU count,
    maximising throughput. CLI flags always override `pyfly.yaml`.

---

## What you built {.recap}

Part I is complete.

In four chapters you went from an empty scaffold to a production-shaped service.
Lumen now **boots** (`@pyfly_application`, startup banner, structured logging),
is **wired** (services and repositories connected through constructor injection
with no glue code), **configured** (four-layer `pyfly.yaml` + profile overlays
+ env-var secrets, typed `WalletProperties`), and **serves** — a validated REST
API at `/api/v1/wallets` with `PathVar`, `QueryParam`, and `Valid[Body[T]]`
body binding, structured 422 errors from Pydantic constraints, domain-error-to-
status mapping from the exception hierarchy, typed response models (`WalletDto`,
`BalanceDto`) that drive OpenAPI schema generation, and a pluggable ASGI server
running underneath.

Every part of this stack follows the same hexagonal principle you have seen
throughout: your code depends on ports and decorators, the framework wires the
adapters. You can swap the in-memory store for a PostgreSQL adapter in Chapter
5, replace the direct dispatch with a full CQRS bus in Chapter 7, or enable XML
responses — none of it requires touching the controller's decorator structure or
the DTO shapes.

Part II will take Lumen further: persistent data with R2DBC and SQLAlchemy,
domain events, resilience with circuit breakers, and security with JWT. The
foundations you laid here carry forward intact.

---

## Try it yourself {.exercises}

1. **Add a `DELETE /api/v1/wallets/{wallet_id}` endpoint.** Remove the wallet
   from `_wallets` and return 204 No Content. Raise `ResourceNotFoundException`
   if the wallet does not exist. Decorate with
   `@delete_mapping("/{wallet_id}", status_code=204)` — PyFly converts a `None`
   return with `status_code=204` into a 204 response with no body. Verify with
   `curl -X DELETE http://localhost:8080/api/v1/wallets/{id}`.

2. **Add currency filtering to `list_wallets`.** Add a
   `currency: QueryParam[str] = None` parameter and filter `_wallets.values()`
   when it is not `None`. Test with
   `GET /api/v1/wallets?currency=EUR` and confirm only EUR wallets are returned;
   confirm `GET /api/v1/wallets` without the parameter returns all wallets.
   Then make the parameter required by removing the default — observe the 400
   response when you omit it from the request.

3. **Raise a domain-specific subclass of `ResourceNotFoundException`.** Create a
   `WalletNotFoundError` that subclasses `ResourceNotFoundException` and carry an
   extra `currency` field in `context`. Raise it from `get_wallet` and
   `get_balance` instead of bare `ResourceNotFoundException`. Verify the JSON
   error response includes the extra context field without any change to the
   global handler — the hierarchy maps it to 404 automatically.
