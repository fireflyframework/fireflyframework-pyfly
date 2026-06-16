<span class="eyebrow">Chapter 4</span>

# Your First HTTP API {.chtitle}

::: figure art/openers/ch04.svg | &nbsp;

Lumen has wired services, a clean configuration story, and a lifecycle that
spans development through production. The only thing missing is a way for the
outside world to talk to it. This chapter closes Part I by turning the wallet
domain into a clean, validated REST API — one with automatic OpenAPI
documentation, structured error responses that clients can trust, and the
framework-managed conventions you have come to expect from the rest of PyFly.

---

## Controllers and route mappings

Every web framework must answer two questions: how does a request find the right
handler, and how does that handler get the dependencies it needs? Frameworks that
answer these questions with separate mechanisms force you to maintain a router
file, a DI glue file, and a documentation scaffold in three different places.
PyFly collapses all three concerns into a single class.

A **controller** in PyFly is an ordinary Python class that the DI container
manages and the web layer routes requests into. Two decorators mark it: 
`@rest_controller` from `pyfly.container` registers it as a bean and sets its
stereotype; `@request_mapping` from `pyfly.web` sets the URL prefix inherited by
every handler in the class.

A few terms in that paragraph will recur throughout the chapter, so let us pin
them down once. A **bean** is simply an object the DI container creates and
hands out for you — you never call `WalletController()` yourself; the framework
constructs it and keeps a single shared instance. A **stereotype** is a label
the framework stamps on a class so it knows *what kind* of bean it is —
`@rest_controller` stamps "this is a web controller", which is the cue the
startup machinery uses to go looking for routes inside it. A **handler** is one
`async def` method on the controller that answers one kind of request. With
those three words in hand, the rest of the chapter reads as plain English.

Route handlers are plain `async def` methods, each decorated with
`@get_mapping`, `@post_mapping`, `@put_mapping`, `@patch_mapping`, or
`@delete_mapping`. Every mapping decorator accepts an optional relative path and
an optional `status_code`. The full URL is the base path from `@request_mapping`
concatenated with the relative path from the method decorator.

### A pre-CQRS reading example

Chapter 7 introduces the full CQRS command/query bus that Lumen uses in
production. Here in Part I the web-layer mechanics are taught on the same wallet
domain, backed by a simple in-memory store instead of the bus. The controller
structure, imports, and decorator shapes are *identical* to Chapter 7; only the
dispatch target changes.

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

Four design choices in this listing are worth examining.

`@rest_controller` does two things at once: it registers `WalletController` as a
singleton bean in the DI container and sets the `__pyfly_stereotype__` marker
that `ControllerRegistrar` uses to discover and mount routes at startup. Pairing
it with `@request_mapping("/api/v1/wallets")` means every method-level decorator
inherits that prefix — you write the base path once.

This version has no `__init__` with injected collaborators. The in-memory
`_wallets` dictionary is a module-level store that suffices for Part I. Chapter 5
introduces repositories; Chapter 7 shows the production pattern: a constructor
that receives `DefaultCommandBus` and `DefaultQueryBus` from the DI container,
dispatching commands and queries through the bus rather than reading `_wallets`
directly.

Each handler returns a Pydantic model (`WalletDto`, `BalanceDto`) or a plain
`dict`. The framework serialises the return value to JSON and sets the
`Content-Type` header — the handler never builds a response object. The
`status_code=201` argument to `@post_mapping` produces a 201 Created on success;
all other handlers default to 200.

All five mapping decorators accept the same two parameters:

| Parameter | Default | Description |
|---|---|---|
| `path` | `""` | Relative path appended to the base. Use `{name}` for path variables. |
| `status_code` | `200` | HTTP status code for a successful response. |

`@post_mapping("", status_code=201)` maps `POST /api/v1/wallets` and returns 201
on success. `@get_mapping("/{wallet_id}")` maps `GET /api/v1/wallets/{wallet_id}`.
Paths are concatenated at startup; duplicate or trailing slashes are normalised
automatically.

### Building the controller, step by step

If you are typing this in from scratch, the listing above lands all at once.
Here is the same controller assembled in the order you would actually build it,
so each decorator has a job to do before the next one arrives.

**Step 1 — Create the file and the class.** Make
`src/lumen/web/controllers/wallet_controller.py` and define an empty class
decorated with the two class-level decorators. This is enough for PyFly to
discover the controller at startup, even before it has a single route.

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, inspect."""
```

**Step 2 — Add the first handler.** Give the class one `async def` method and
mark it with a mapping decorator. `@post_mapping("", status_code=201)` maps
`POST /api/v1/wallets` — the empty path means "the base path with nothing
appended" — and promises a `201 Created` on success.

**Step 3 — Add the remaining handlers.** Repeat the pattern: one `async def`
per route, each with its own mapping decorator and relative path. The full set
in Listing 4.1 gives you open, fetch, balance, deposit, and list.

**Step 4 — Wire the store.** The module-level `_wallets` dictionary is the only
"database" Part I needs. Each handler reads from and writes to it directly;
Chapter 5 swaps it for a real repository without touching a single decorator.

!!! note "Note"
    Notice what you did *not* write: no router file mapping URLs to functions,
    no registration call in `main.py`, no manual OpenAPI entry. The decorators
    are the registration. At startup `ControllerRegistrar` finds every
    `@rest_controller` bean and mounts its routes for you.

!!! tip "Run it"
    Start the server and confirm the routes are live. From the project root:

    ```bash
    uv run pyfly run --server uvicorn
    ```

    The boot banner reports the framework version and the bound port:

    ```
    :: PyFly Framework :: (v26.06.110) (Python 3.13.13)
    ```

    In a second terminal, open a wallet and read it back:

    ```bash
    curl -s -X POST localhost:8080/api/v1/wallets \
      -H 'Content-Type: application/json' \
      -d '{"owner_id": "alice", "currency": "EUR"}'
    ```

    You should see a `201` body with the generated id:

    ```json
    {"wallet_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
    ```

    Copy that id and fetch the wallet:

    ```bash
    curl -s localhost:8080/api/v1/wallets/a1b2c3d4-e5f6-7890-abcd-ef1234567890
    ```

    ```json
    {"id": "a1b2c3d4-...", "owner_id": "alice", "currency": "EUR",
     "balance_minor": 0, "balance": 0.0, "created_at": "2026-06-15T10:30:00+00:00"}
    ```

**What just happened.** Two class-level decorators registered the controller and
fixed its URL prefix; one method-level decorator turned an `async def` into a
live route; the framework did the routing, JSON serialisation, and status-code
handling. You wrote business intent, not plumbing.

::: figure art/figures/04-request.svg | Figure 4.1 — How a request flows to your handler.

!!! spring "Spring parity"
    `@rest_controller` + `@request_mapping` + `@get_mapping` / `@post_mapping`
    is a direct translation of Spring's `@RestController` + `@RequestMapping` +
    `@GetMapping` / `@PostMapping`. Handler methods return values directly (not
    `ResponseEntity`) and the framework converts them to JSON — exactly the
    pattern Spring encourages with `@ResponseBody` on `@RestController`.

---

## Binding request data

A request carries data in several places at once: the URL path identifies the
resource, the query string carries filters and pagination, the body carries the
payload, and headers carry metadata. Most frameworks address these with separate
mechanisms, each with its own conventions. PyFly unifies them under a single
idea: **generic type annotations on handler parameters declare where data comes
from**.

This approach makes handler signatures self-documenting. The parameter list of
any handler tells you exactly which parts of the request it reads and what types
it expects — without opening a router file or consulting the docs.

In plain terms, **binding** is the framework copying a piece of the incoming
request into one of your handler's parameters, converting it to the type you
asked for along the way. You declare *what you want and where it comes from* with
a type annotation; PyFly does the extracting, parsing, and type-coercion before
your method body runs.

The `ParameterResolver` inspects each handler signature at startup and builds a
resolution plan, so there is zero overhead per request for introspection. Five
binding types cover every part of an HTTP request:

### PathVar[T] — path variables

Extracts a named segment from the URL path. The parameter name must match a
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
`PathVar[float]`, and `PathVar[UUID]` all work — the coercion calls `int(value)`,
`float(value)`, and `UUID(value)` respectively.

### QueryParam[T] — query parameters

Extracts a value from the query string, with support for defaults and optional
values.

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
admit `None`. A missing required `QueryParam` raises `InvalidRequestException`
(HTTP 400). To make a parameter optional, give it a default value or annotate it
`QueryParam[str | None]`.

!!! tip "Run it"
    With the server running and at least one wallet opened, exercise the
    `list_wallets` handler — first with no filter, then with the optional
    `owner_id` query parameter:

    ```bash
    curl -s 'localhost:8080/api/v1/wallets'
    curl -s 'localhost:8080/api/v1/wallets?owner_id=alice'
    ```

    The first returns every wallet; the second returns only Alice's. Because
    `owner_id` has a default of `None`, omitting it is perfectly valid — no 400.
    The path variable behaves the same way in reverse: ask for a wallet id that
    does not exist and you get a clean `404`, which the next section dissects.

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

Extract values from request headers and cookies. For headers, the parameter name
is converted from `snake_case` to `kebab-case` automatically:

```python
@get_mapping("/me")
async def get_my_wallets(
    self,
    x_api_key: Header[str],
    session_id: Cookie[str | None],
) -> list[WalletDto]:
    ...
```

`x_api_key: Header[str]` reads the `x-api-key` header. A missing required header
or cookie raises `InvalidRequestException` (HTTP 400), the same as a missing
query parameter.

!!! tip "Tip"
    All five binding types follow the same **required vs optional** rule: no
    default + non-`None` type = required (HTTP 400 when absent); any default or
    `T | None` = optional. The rule is uniform across `QueryParam`, `Header`,
    and `Cookie` — you learn it once, it applies everywhere.

**What just happened.** You learned the whole binding vocabulary as five
parallel annotations — `PathVar`, `QueryParam`, `Body`, `Header`, `Cookie` —
that all read like English in a handler signature and all share one
required-vs-optional rule. The framework reads the annotation, pulls the value
from the right place, coerces it to your type, and hands you a ready-to-use
argument. There is nothing else to wire.

---

## Validation with Valid[T]

Binding tells the framework *where* data comes from. Validation tells it *what
that data must look like* before your handler ever sees it. Without a layer that
intercepts bad input early, validation logic scatters into service methods,
manual `if` blocks litter business code, and different handlers produce
inconsistent error responses depending on where they happen to catch the problem.

PyFly solves this at the type level. Pydantic `BaseModel` gives you field-level
constraints for free. `Valid[T]` is PyFly's marker that converts a Pydantic
`ValidationError` into a **structured 422 response** instead of letting it
bubble up to a 500.

A quick gloss before the code. A **DTO** — Data Transfer Object — is a small
class that describes the *shape* of data crossing the wire: what fields a request
must carry, or what fields a response will return. Lumen's DTOs are plain
Pydantic models, so the field declarations double as validation rules.
**Validation** is the act of checking incoming data against those rules and
rejecting it cleanly if it does not fit — before any of your handler code runs.

### Pydantic DTOs for Lumen

The request and response DTOs used in Lumen's wallet API live under
`lumen/interfaces/dtos/v1/` — one file per DTO. The directory name encodes a
convention worth noting: `interfaces` holds the contracts the outside world sees,
and `v1` versions them so a future `v2` payload shape can live alongside the old
one without breaking existing clients. Here they are in full.

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

These are pure Pydantic models — PyFly adds nothing to them. Four design
decisions are worth unpacking.

`OpenWalletRequest.owner_id` uses `Field(min_length=1, max_length=64)`. The
lower bound prevents phantom wallets from empty-string owner IDs that would
silently pollute your data; the upper bound keeps identifiers within a sensible
column width when the database layer arrives in Chapter 5.

`currency` is a `Currency` enum (a `StrEnum` with `EUR`, `USD`, `GBP`). Using
an enum rather than a raw string means Pydantic rejects `"XYZ"` at
deserialisation time — you never validate the currency code yourself.
`Field(default=Currency.EUR)` provides a sensible default so callers can omit
the field for EUR wallets.

`DepositRequest.amount` is an `int` with `Field(gt=0)`. Storing money in minor
units avoids floating-point rounding: `1050` means €10.50 for an EUR wallet.
The `gt=0` constraint makes a zero or negative deposit a 422 client error, not
a business-logic decision — the constraint lives in the type, and Pydantic
enforces it before your handler runs.

`WalletDto` and `BalanceDto` are response models. Returning a typed Pydantic
model instead of a plain `dict` lets the framework generate accurate OpenAPI
response schemas and gives clients a machine-readable contract.

### Using Valid[T] in a handler

Wrap `Body[T]` in `Valid` to opt into structured 422 errors on validation
failure:

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
request body (`Body`) and run Pydantic validation before the handler executes
(`Valid`). When the body fails validation, the resolver catches the
`ValidationError` and raises a `ValidationException` with
`code="VALIDATION_ERROR"` and a `context.errors` array containing each
field-level detail.

### What the client sees on failure

To see validation in action, send a `POST /api/v1/wallets` with an empty
`owner_id`:

```
POST /api/v1/wallets
Content-Type: application/json

{"owner_id": ""}
```

!!! tip "Run it"
    With the server running, send the bad payload and watch for the `422`:

    ```bash
    curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8080/api/v1/wallets \
      -H 'Content-Type: application/json' \
      -d '{"owner_id": ""}'
    ```

    The `-w '\nHTTP %{http_code}\n'` flag prints the status line after the body,
    so you can confirm it is `HTTP 422` — not the `201` a valid request returns,
    and not a `500`. The body is the structured envelope shown below. Try a
    second variant — `-d '{"owner_id": "alice", "currency": "XYZ"}'` — to see the
    `Currency` enum reject an unknown code with the same envelope shape.

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

Every field error carries a `type` (machine-readable), a `loc` (path to the
failing field), a `msg` (human-readable), and an `input` (the rejected value).
API consumers parse this array deterministically — no scraping of error strings.

The difference between bare `Body[T]` and `Valid[Body[T]]` is exactly this:

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

**What just happened.** The validation rules never left the DTO. `Field(min_length=1)`
on `owner_id`, the `Currency` enum, and `Field(gt=0)` on `amount` are the entire
specification — and wrapping the body in `Valid` turned any breach of those rules
into a predictable, machine-readable `422` before your handler ran. You wrote
constraints once, on the data; the framework enforced them everywhere the data
arrives.

---

## Errors that clients can trust

A well-designed API fails loudly, consistently, and informatively. Clients must
never parse exception stack traces or guess what went wrong from a generic 500.
The challenge is achieving this without scattering HTTP-specific logic through
your service code — the HTTP status code is an infrastructure concern, not a
business one.

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
the system's fault. Subclasses pin the status code. When a new domain error does
not fit an existing subclass, extend the nearest parent and the status code comes
for free.

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

!!! tip "Run it"
    Ask for a wallet that was never opened and watch the framework turn your
    `raise` into a clean `404`:

    ```bash
    curl -s -w '\nHTTP %{http_code}\n' localhost:8080/api/v1/wallets/w-999
    ```

    The status line reads `HTTP 404` and the body is the envelope above, with
    `"code": "WALLET_NOT_FOUND"` and your `context` carried through verbatim. You
    never wrote a status code in `get_wallet` — `ResourceNotFoundException` maps
    to 404 for you. Note the `transaction_id` in the response; copy it and grep
    your server log to find the exact request.

The `transaction_id` is free: the `TransactionIdFilter` assigns a UUID to every
request and threads it through all error responses. Clients log it; support uses
it to find the corresponding server log entry. A single ID is all that is needed
to reconstruct what happened.

**What just happened.** Your handler expressed a domain fact — "this wallet does
not exist" — by raising a typed exception with a message, a code, and some
context. The web layer's global handler did the HTTP translation: it picked the
status code from the exception's class, wrapped everything in the standard error
envelope, and stamped a `transaction_id`. HTTP concerns stayed out of your
business code entirely.

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

Returning a `dict` or a Pydantic model is not quite the end of the story.
Somewhere between your handler's `return` statement and the bytes the client
receives, the framework decides on a wire format. Rather than hardcoding JSON,
PyFly runs the return value through an ordered `HttpMessageConverter` chain —
important for enterprise APIs that must serve XML partners or negotiate the
lightest format for mobile clients.

JSON is the default. When no `Accept` header is sent, the response is
`application/json`. When the client sends `Accept: application/xml`, the XML
converter takes over and serialises the same return value as XML, with no change
to your handler code:

```
GET /api/v1/wallets/w-001   Accept: application/json
  →  {"id": "w-001", ...}

GET /api/v1/wallets/w-001   Accept: application/xml
  →  <response><id>w-001</id>...</response>
```

The same negotiation applies to inbound data: a `Body[T]` or `Valid[Body[T]]`
parameter accepts both `Content-Type: application/json` and
`Content-Type: application/xml` request bodies. JSON is the fallback when no
`Content-Type` header is present.

### Auto-generated documentation

Manually maintained API specs drift. As routes change, parameters are renamed,
and new models are added, hand-written specs fall behind the code. PyFly
eliminates this entirely by generating documentation from the same metadata that
drives routing — the spec is always in sync because it is the same source.

As soon as Lumen starts, three documentation endpoints are live at no cost:

| Endpoint | Purpose |
|---|---|
| `/docs` | Swagger UI — interactive, try-it-now documentation |
| `/redoc` | ReDoc — clean, two-panel reference documentation |
| `/openapi.json` | Raw OpenAPI 3.0 specification |

The `OpenAPIGenerator` introspects `ControllerRegistrar`'s route metadata —
every path, method, path variable, query parameter, and request/response schema
(from Pydantic model introspection) — and assembles the spec at startup. You
never write the spec by hand. The docs endpoints live on the **application**
port (8080) alongside your API; they are on by default (`pyfly.web.docs.enabled:
true`). Disable them in production with `pyfly.web.docs.enabled: false` in
`pyfly.yaml`.

!!! note "Note"
    Do not confuse the docs endpoints with the **admin dashboard**. `/docs`,
    `/redoc`, and `/openapi.json` describe *your* API and serve on the app port
    (8080). The PyFly Admin Dashboard (`/admin`) and the actuator health
    endpoints (`/actuator/*`) describe the *running process* and serve on the
    separate **management** port (`pyfly.management.server.port`, default 9090),
    introduced in Chapter 3. They are two different listeners with two different
    audiences.

!!! tip "Run it"
    With the server running, fetch the raw spec and confirm your routes are in
    it:

    ```bash
    curl -s localhost:8080/openapi.json | head -c 200
    ```

    You will see the OpenAPI 3.0 header and the start of the `paths` map. Then
    open `http://localhost:8080/docs` in a browser. You will see
    `POST /api/v1/wallets`, `GET /api/v1/wallets/{wallet_id}`,
    `POST /api/v1/wallets/{wallet_id}/deposit`, and the others — each with the
    correct request and response schemas derived from your Pydantic models, and
    the `owner_id` query parameter on `list_wallets` already documented with its
    type and default. Click "Try it out" on `POST /api/v1/wallets` to open a
    real wallet straight from the browser.

**What just happened.** You did not write a line of API documentation, yet a
complete, interactive, always-accurate spec appeared. The same route and model
metadata that drives request handling also drives the docs, so the two can never
drift apart.

---

## The server underneath

Lumen now has routes, bindings, validation, and documentation. The last question
is what actually listens on port 8080. The answer matters: different servers make
different trade-offs in throughput, HTTP version support, OS compatibility, and
ecosystem tooling. Locking an application to a single server at the framework
level forces you to accept those trade-offs permanently.

An **ASGI server** is the process that actually accepts TCP connections, parses
HTTP, and calls your application — the layer between the operating system's
socket and your handlers. PyFly does not hardcode one. At startup,
`ServerAutoConfiguration` runs a cascading selection based on what is installed:

| Priority | Server | Characteristic |
|---|---|---|
| 1st | **Granian** | Rust/tokio-powered; fastest single-worker throughput |
| 2nd | **Uvicorn** | Ecosystem standard; best tooling support |
| 3rd | **Hypercorn** | Native HTTP/2 and HTTP/3 |

All three start through the same `ApplicationServerPort` protocol, so your code
is completely unaware of which one is running. Override with
`pyfly.server.type: uvicorn` in `pyfly.yaml` or with the `--server` CLI flag:

```bash
pyfly run --server uvicorn --reload      # development: auto-reload
pyfly run --server granian --workers 4  # production: multi-worker
```

!!! tip "Run it"
    For day-to-day development, run with auto-reload so the server restarts on
    every save:

    ```bash
    uv run pyfly run --reload
    ```

    PyFly logs the chosen server and the bound port at startup. Because
    `--reload` requires a built-in file watcher, PyFly selects **Uvicorn** for
    reload mode regardless of the cascade order. Edit a handler, save, and watch
    the log report the restart — then re-run any `curl` from earlier and see your
    change live without stopping the process.

The event loop is pluggable too: `uvloop` (Linux/macOS) and `winloop` (Windows)
are selected automatically when installed, delivering a 2–4× throughput
improvement over the asyncio default. Install them with `uv add "pyfly[web-fast]"`.

!!! tip "Tip"
    For development, `pyfly run --reload` is all you need — it picks the best
    available server and event loop automatically. For production, pass an
    explicit positive worker count to scale across cores —
    `pyfly run --server granian --workers 4`, as in the example above. A `0`
    or negative `--workers` value resolves to a single worker, so multi-worker
    is always an explicit opt-in. CLI flags always override `pyfly.yaml`.

---

## What you built {.recap}

Part I is complete.

In four chapters you went from an empty scaffold to a production-shaped service.
Lumen now **boots** (`@pyfly_application`, startup banner, structured logging),
is **wired** (services and repositories connected through constructor injection
with no glue code), **configured** (four-layer `pyfly.yaml` + profile overlays
+ env-var secrets, typed `WalletProperties`), and **serves** — a validated REST
API at `/api/v1/wallets` with `PathVar`, `QueryParam`, and `Valid[Body[T]]`
body binding; structured 422 errors from Pydantic constraints; domain-error-to-
status mapping from the exception hierarchy; typed response models (`WalletDto`,
`BalanceDto`) that drive OpenAPI schema generation; and a pluggable ASGI server
running underneath.

Every part of this stack follows the same hexagonal principle you have seen
throughout: your code depends on ports and decorators; the framework wires the
adapters. Swap the in-memory store for a PostgreSQL adapter in Chapter 5, replace
direct dispatch with a full CQRS bus in Chapter 7, or enable XML responses —
none of it requires touching the controller's decorator structure or the DTO
shapes.

Part II takes Lumen further: persistent data with SQLAlchemy, domain events,
resilience with circuit breakers, and security with JWT. The foundations you
built here carry forward intact.

---

## Try it yourself {.exercises}

Each exercise is small and self-contained. After every change, restart with
`uv run pyfly run --reload` and re-run the suggested `curl` to confirm the
behaviour. If you have the dev dependencies installed, you can also run the
project's test suite at any point to make sure nothing regressed:

```bash
uv run --extra dev pytest
```

You should see a row of passing dots and a `passed` summary line.

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
