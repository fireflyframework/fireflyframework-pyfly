# CQRS Guide

PyFly provides a production-ready CQRS (Command Query Responsibility
Segregation) module built on hexagonal architecture principles. Commands
(write operations) and queries (read operations) flow through dedicated bus
pipelines that handle correlation, validation, authorization, caching,
metrics, and domain event publishing automatically.

---

## Table of Contents

1. [Why CQRS?](#why-cqrs)
2. [Architecture Overview](#architecture-overview)
3. [Commands](#commands)
4. [Queries](#queries)
5. [Command Handlers](#command-handlers)
6. [Query Handlers](#query-handlers)
7. [Handler Decorators](#handler-decorators)
8. [CommandBus](#commandbus)
9. [QueryBus](#querybus)
10. [Handler Registry](#handler-registry)
11. [Validation](#validation)
12. [Authorization](#authorization)
13. [Execution Context](#execution-context)
14. [Distributed Tracing](#distributed-tracing)
15. [Caching](#caching)
16. [Domain Events](#domain-events)
17. [Fluent Builders](#fluent-builders)
18. [Configuration Reference](#configuration-reference)
19. [Auto-Configuration](#auto-configuration)
20. [Actuator Endpoints](#actuator-endpoints)
21. [Complete Example: Order Management](#complete-example-order-management)
22. [Testing CQRS Components](#testing-cqrs-components)

---

## Why CQRS?

* **Commands** change state, passing through validation, authorization, and event publishing.
* **Queries** read state, passing through validation, authorization, and an integrated cache layer.

This separation lets you optimize reads and writes independently, apply
different security policies, and scale each path on its own terms.

---

## Architecture Overview

```
                   send()                   query()
              +----------+             +-----------+
              | CommandBus|             |  QueryBus |
              +----+-----+             +-----+-----+
                   |                         |
     1. Correlate  |           1. Correlate  |
     2. Validate   |           2. Validate   |
     3. Authorize  |           3. Authorize  |
     4. Execute    |           4. Cache check |
     5. Publish    |           5. Execute     |
     6. Metrics    |           6. Cache put   |
                   |           7. Metrics     |
              +----v-----+             +-----v-----+
              | Handler  |             |  Handler   |
              +----------+             +-----------+
```

Both buses delegate handler lookup to a shared `HandlerRegistry` that
discovers handlers automatically via `@command_handler` / `@query_handler`
decorator markers.

---

## Commands

A `Command[R]` represents a write operation whose result type is `R`.

```python
from dataclasses import dataclass
from pyfly.cqrs.types import Command

@dataclass(frozen=True)
class CreateOrderCommand(Command[str]):
    customer_id: str
    items: list[str]
    total: float
```

### Metadata API

Metadata uses `object.__setattr__`, so it works safely with `frozen=True`.

| Method | Return Type | Description |
|--------|-------------|-------------|
| `get_command_id()` | `str` | Auto-generated UUID. |
| `get_correlation_id()` / `set_correlation_id(id)` | `str \| None` | Distributed tracing correlation. |
| `get_timestamp()` | `datetime` | UTC creation time. |
| `get_initiated_by()` / `set_initiated_by(user_id)` | `str \| None` | Who initiated the command. |
| `get_metadata()` / `set_metadata(key, value)` | `dict[str, Any]` | Arbitrary key-value pairs. |

### Pipeline Hooks

| Method | Default | Description |
|--------|---------|-------------|
| `validate()` | `ValidationResult.success()` | Custom business-rule validation. |
| `authorize()` | `AuthorizationResult.success()` | Authorization check. |
| `authorize_with_context(ctx)` | Delegates to `authorize()` | Authorization with `ExecutionContext`. |
| `get_cache_key()` | `None` | Cache-invalidation key: a query's `get_cache_key()` whose cached results the command makes stale. The command bus evicts it after the command commits (see [Command-side invalidation](#command-side-invalidation)). |

---

## Queries

A `Query[R]` represents a read operation whose result type is `R`.

```python
from dataclasses import dataclass
from pyfly.cqrs.types import Query

@dataclass(frozen=True)
class GetOrderQuery(Query[dict | None]):
    order_id: str
```

### Metadata API

| Method | Return Type | Description |
|--------|-------------|-------------|
| `get_query_id()` | `str` | Auto-generated UUID. |
| `get_correlation_id()` / `set_correlation_id(id)` | `str \| None` | Distributed tracing correlation. |
| `get_timestamp()` | `datetime` | UTC creation time. |
| `get_metadata()` | `dict[str, Any]` | Arbitrary metadata. |
| `is_cacheable()` / `set_cacheable(bool)` | `bool` | Whether results can be cached (default `True`). |
| `get_cache_key()` | `str \| None` | Cache key. For dataclass subclasses: `ClassName:sha256_hex16(fields)` — a stable SHA-256 digest so the same query maps to the same key across processes. For non-dataclass subclasses: the class name. Override to provide a fully custom key. |

Queries share the same `validate()`, `authorize()`, and `authorize_with_context(ctx)` hooks as commands.

---

## Command Handlers

`CommandHandler[C, R]` is a generic base class with a template-method
pipeline. Subclasses **must** implement `do_handle()`.

```python
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.container import service

@command_handler
@service
class CreateOrderHandler(CommandHandler[CreateOrderCommand, str]):
    def __init__(self, repo: OrderRepository) -> None:
        self._repo = repo

    async def do_handle(self, command: CreateOrderCommand) -> str:
        order = Order(customer_id=command.customer_id, items=command.items)
        return await self._repo.save(order)
```

### Lifecycle Hooks

| Hook | Called | Default |
|------|--------|---------|
| `pre_process(command)` | Before `do_handle`. | No-op. |
| `do_handle(command)` | Core business logic. | **Must override.** |
| `post_process(command, result)` | After `do_handle` on success. | No-op. |
| `on_success(command, result)` | After `post_process`. | No-op. |
| `on_error(command, error)` | When `do_handle` raises. | Logs the error. |
| `map_error(command, error)` | Transform exception before propagation. | Returns the original error. |

### ContextAwareCommandHandler

Extend `ContextAwareCommandHandler` when your handler **requires** an
`ExecutionContext`. Calling `handle()` raises `RuntimeError`; callers must
use `handle_with_context()`.

```python
from pyfly.cqrs.command.handler import ContextAwareCommandHandler
from pyfly.cqrs.context.execution_context import ExecutionContext

@command_handler
@service
class TransferFundsHandler(ContextAwareCommandHandler[TransferFundsCommand, str]):
    async def do_handle_with_context(
        self, command: TransferFundsCommand, context: ExecutionContext
    ) -> str:
        return f"transfer-for-{context.user_id}"
```

---

## Query Handlers

`QueryHandler[Q, R]` follows the same template-method pattern. It adds
caching metadata methods: `supports_caching()`, `get_cache_ttl_seconds()`,
`get_cache_key_prefix()`, `get_cache_scope()`, `caches_none()`,
`get_cache_evict_events()` and `get_result_type()` (the declared `R`).

```python
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.decorators import query_handler
from pyfly.container import service

@query_handler(cacheable=True, cache_ttl=300)
@service
class GetOrderHandler(QueryHandler[GetOrderQuery, OrderDto | None]):
    def __init__(self, repo: OrderRepository) -> None:
        self._repo = repo

    async def do_handle(self, query: GetOrderQuery) -> OrderDto | None:
        order = await self._repo.find_by_id(query.order_id)
        # A cacheable handler returns a DTO: an ORM entity is never cached.
        return OrderDto.model_validate(order, from_attributes=True) if order else None
```

Lifecycle hooks are identical to `CommandHandler`. Use
`ContextAwareQueryHandler` when a context is required.

---

## Handler Decorators

### @command_handler

```python
from pyfly.cqrs.decorators import command_handler

@command_handler                                          # bare
@command_handler(timeout=30, retries=2, backoff_ms=500)   # parameterized
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timeout` | `int \| None` | `None` | Max execution time (seconds). |
| `retries` | `int` | `0` | Retry attempts on failure. |
| `backoff_ms` | `int` | `1000` | Backoff between retries (ms). |
| `metrics` | `bool` | `True` | Enable metrics. |
| `tracing` | `bool` | `True` | Enable tracing. |
| `validation` | `bool` | `True` | Enable validation. |
| `priority` | `int` | `0` | Lower = higher priority. |
| `tags` | `tuple[str, ...]` | `()` | Arbitrary tags. |
| `description` | `str` | `""` | Description. |

### @query_handler

```python
from pyfly.cqrs.decorators import query_handler

@query_handler                                                    # bare
@query_handler(cacheable=True, cache_ttl=600, cache_key_prefix="orders")  # parameterized
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timeout` | `int \| None` | `None` | Max execution time (seconds). |
| `retries` | `int` | `0` | Retry attempts. |
| `metrics` | `bool` | `True` | Enable metrics. |
| `tracing` | `bool` | `True` | Enable tracing. |
| `cacheable` | `bool` | `False` | Enable result caching. |
| `cache_ttl` | `int \| None` | `None` | Cache TTL (seconds). |
| `cache_key_prefix` | `str \| None` | `None` | Key prefix: results are stored under `<prefix>:<query cache key>`, and the prefix names the group an event-tag eviction removes. |
| `priority` | `int` | `0` | Lower = higher priority. |
| `tags` | `tuple[str, ...]` | `()` | Arbitrary tags. |
| `description` | `str` | `""` | Description. |

---

## CommandBus

`CommandBus` is a `@runtime_checkable Protocol` with `send()`,
`send_with_context()`, `register_handler()`, `unregister_handler()`, and
`has_handler()` methods.

### DefaultCommandBus

Pipeline: correlate, validate, authorize, execute, invalidate the query cache (with a query cache, see
[Command-side invalidation](#command-side-invalidation)), publish events, record metrics.

```python
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.registry import HandlerRegistry

registry = HandlerRegistry()
bus = DefaultCommandBus(registry=registry)
bus.register_handler(create_order_handler)

order_id = await bus.send(CreateOrderCommand(customer_id="cust-1", items=["A"], total=9.99))
```

| Constructor Param | Type | Default |
|-------------------|------|---------|
| `registry` | `HandlerRegistry` | *required* |
| `validation` | `CommandValidationService \| None` | `None` |
| `authorization` | `AuthorizationService \| None` | `None` |
| `metrics` | `CqrsMetricsService \| None` | `None` |
| `event_publisher` | `Any \| None` | `None` |

Failures are wrapped in `CommandProcessingException`.

---

## QueryBus

`QueryBus` is a `@runtime_checkable Protocol` with `query()`,
`query_with_context()`, `register_handler()`, `unregister_handler()`,
`has_handler()`, `clear_cache()`, and `clear_all_cache()`.

### DefaultQueryBus

Pipeline: correlate, validate, authorize, cache check, execute, cache put, record metrics.

```python
from pyfly.cqrs.query.bus import DefaultQueryBus

bus = DefaultQueryBus(registry=registry, default_cache_ttl=900)
order = await bus.query(GetOrderQuery(order_id="ord-123"))
```

| Constructor Param | Type | Default |
|-------------------|------|---------|
| `registry` | `HandlerRegistry` | *required* |
| `validation` | `CommandValidationService \| None` | `None` |
| `authorization` | `AuthorizationService \| None` | `None` |
| `metrics` | `CqrsMetricsService \| None` | `None` |
| `cache_adapter` | `Any \| None` | `None` |
| `default_cache_ttl` | `int` | `900` |

Cache keys are prefixed with `:cqrs:`. Failures are wrapped in `QueryProcessingException`.

---

## Handler Registry

`HandlerRegistry` stores handlers keyed by message type.

```python
from pyfly.cqrs.command.registry import HandlerRegistry

registry = HandlerRegistry()
registry.register_command_handler(handler)
registry.register_query_handler(handler)
handler = registry.find_command_handler(CreateOrderCommand)  # raises if missing
```

| Method | Description |
|--------|-------------|
| `register_command_handler(handler)` | Register by introspected command type. |
| `register_query_handler(handler)` | Register by introspected query type. |
| `find_command_handler(type)` | Lookup. Raises `CommandHandlerNotFoundException`. |
| `find_query_handler(type)` | Lookup. Raises `QueryHandlerNotFoundException`. |
| `has_command_handler(type)` / `has_query_handler(type)` | Existence check. |
| `discover_from_beans(beans)` | Scan beans for `@command_handler`/`@query_handler` markers. |
| `command_handler_count` / `query_handler_count` | Registered handler counts. |

---

## Validation

Two-phase pipeline: pydantic structural validation, then custom `validate()`.

```python
from pyfly.cqrs.validation.types import ValidationResult, ValidationError, ValidationSeverity
```

| Type | Fields |
|------|--------|
| `ValidationResult` | `valid: bool`, `errors: tuple[ValidationError, ...]` |
| `ValidationError` | `field_name`, `message`, `error_code`, `severity`, `rejected_value` |
| `ValidationSeverity` | `WARNING`, `ERROR`, `CRITICAL` |

Factory methods: `ValidationResult.success()`, `.failure(field, message)`,
`.from_errors(list)`. Combine with `result.combine(other)`.

The `AutoValidationProcessor` runs pydantic validation (if the object is a
`BaseModel`) and the object's `validate()` method, then merges results.
Override `validate()` on your command or query to add business rules:

```python
@dataclass(frozen=True)
class CreateOrderCommand(Command[str]):
    customer_id: str
    total: float

    async def validate(self) -> ValidationResult:
        if self.total <= 0:
            return ValidationResult.failure("total", "Must be positive")
        return ValidationResult.success()
```

On failure the bus raises `CqrsValidationException`.

---

## Authorization

Runs after validation. The `AuthorizationService` calls the message's
`authorize_with_context(ctx)` or `authorize()` hooks.

```python
from pyfly.cqrs.authorization.types import AuthorizationResult, AuthorizationError, AuthorizationSeverity
```

| Type | Fields |
|------|--------|
| `AuthorizationResult` | `authorized: bool`, `errors: tuple[AuthorizationError, ...]` |
| `AuthorizationError` | `resource`, `message`, `error_code`, `severity`, `denied_action` |

Factory methods: `AuthorizationResult.success()`, `.failure(resource, message)`.
Combine with `result.combine(other)`.

`AuthorizationService(enabled=True)` evaluates hooks; when `enabled=False`
all requests are auto-authorized. On denial raises `AuthorizationException`.

```python
@dataclass(frozen=True)
class DeleteOrderCommand(Command[bool]):
    order_id: str
    requested_by: str

    async def authorize(self) -> AuthorizationResult:
        if self.requested_by == "admin":
            return AuthorizationResult.success()
        return AuthorizationResult.failure("order", "Only admins can delete orders")
```

---

## Execution Context

`ExecutionContext` is a `@runtime_checkable Protocol` carrying user
identity, tenant, request metadata, feature flags, and properties.

| Property | Type |
|----------|------|
| `user_id`, `tenant_id`, `organization_id` | `str \| None` |
| `session_id`, `request_id`, `source`, `client_ip`, `user_agent` | `str \| None` |
| `created_at` | `datetime` |
| `properties` | `dict[str, Any]` |
| `feature_flags` | `dict[str, bool]` |

`DefaultExecutionContext` is a frozen dataclass implementation. Use
`ExecutionContextBuilder` for construction:

```python
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder

ctx = (
    ExecutionContextBuilder()
    .with_user_id("user-42")
    .with_tenant_id("tenant-a")
    .with_feature_flag("new-checkout", True)
    .build()
)
order_id = await command_bus.send_with_context(command, ctx)
```

---

## Distributed Tracing

`CorrelationContext` manages correlation, trace, and span IDs via
`contextvars`, propagating correctly across `await` chains.

```python
from pyfly.cqrs.tracing.correlation import CorrelationContext
```

| Method | Description |
|--------|-------------|
| `set_correlation_id(id)` / `get_correlation_id()` | Manage correlation ID. |
| `get_or_create_correlation_id()` | Get or auto-generate. |
| `set_trace_id(id)` / `get_trace_id()` | Manage trace ID. |
| `set_span_id(id)` / `get_span_id()` | Manage span ID. |
| `create_context_headers()` | Build outbound headers (`X-Correlation-ID`, `X-Trace-ID`, `X-Span-ID`). |
| `extract_context_from_headers(headers)` | Restore from inbound headers. |
| `clear()` | Reset all context vars. |

> **Key Point:** Both buses set the correlation ID at the start of every
> pipeline execution and restore the prior correlation ID in a `finally`
> block, so an outer (e.g. per-request) correlation ID is never clobbered
> by nested command dispatches.

---

## Caching

`QueryCacheAdapter` bridges pyfly's cache module with CQRS. The query cache is
the `:cqrs:` [region](caching.md#named-caches-regions-and-dedicated-caches) of
the application's `CacheAdapter` bean. Without an underlying `CacheAdapter`
bean, all operations are silent no-ops (the query bus still works — results are
just not cached).

```python
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
adapter = QueryCacheAdapter(cache=my_cache_instance)
```

| Method | Description |
|--------|-------------|
| `get(key)` | Fetch cached value. |
| `lookup(key, none_cached=True)` | `(found, value)`: tells a stored `None` from a miss (with `none_cached=False` a `None` is a miss, and a miss costs no existence check). |
| `entry_key(key, digest, ttl=None)` | The key an entry lives under for a caller's scope digest; `None` when the call is not cached. |
| `put(key, value, ttl)` | Store with optional `timedelta` TTL; after the commit inside a unit of work. |
| `evict(key)` | Remove a key for every caller's scope; after the commit inside a unit of work. |
| `evict_keys(keys)` | `evict` for several keys as one step of concurrent deletes. |
| `evict_prefix(prefix)` | Remove every entry whose key starts with `prefix` (scans the query cache's keys: `SCAN` on Redis). |
| `clear()` | Remove every query-cache entry (the `:cqrs:` prefix), and nothing else the cache holds. |
| `is_available` | Whether an underlying cache is configured. |

A cache failure is logged and never fails the query or the command that caused
it.

Enable caching on a handler: `@query_handler(cacheable=True, cache_ttl=600)`.
The query must also have `is_cacheable()` return `True` (the default), and the
cache must see who the caller is, unless the handler declares
`QueryCacheScope.GLOBAL` (see [Whose results an entry holds](#whose-results-an-entry-holds)):
an anonymous call to a `USER`-scoped handler is not cached. Invalidate
programmatically via `await query_bus.clear_cache(query.get_cache_key())` (the
key for every caller, and under every handler's `cache_key_prefix`) or
`await query_bus.clear_all_cache()` (the query cache only: orchestration state,
idempotency records and `@cacheable` entries in the same cache are left alone).

When `pyfly.cqrs.enabled=true` and a `CacheAdapter` bean is present, the
`query_cache_adapter` bean is wired automatically by `CqrsAutoConfiguration`
and injected into the `DefaultQueryBus` and the `DefaultCommandBus`. No extra
configuration is required. `pyfly.cqrs.query.caching_enabled: false` turns the
query cache off.

### Whose results an entry holds

A cached result is never served to another tenant or user. The bus keys an
entry by the query's `get_cache_key()` and by the caller, as far as the cache
can trust it (an empty identifier counts as none):

* the tenant, organization and user of the `ExecutionContext` passed to
  `query_with_context()` (your application built it);
* the authenticated principal of the request (`RequestContext.security_context`,
  set by the bearer-token security middleware), so a plain `query()` behind the
  web layer is keyed too;
* the `X-Tenant-Id` header of the request. Any client can send it and nothing
  authenticates it, so it only *narrows* an entry: it is part of the key, but
  it never identifies a caller by itself.

A handler's cache scope decides who shares an entry, and which identity it
needs:

| Scope | Keyed by | Needs | Use it for |
|-------|----------|-------|------------|
| `QueryCacheScope.USER` (default) | tenant, organization and user | a user: the context's or the principal | anything that may depend on who asks |
| `QueryCacheScope.TENANT` | the context's tenant and organization | a tenant or organization in the context, or else a user (then it is keyed by the user) | data shared by the users of one tenant |
| `QueryCacheScope.GLOBAL` | nothing | nothing | data that is the same for everyone (a country list) |

The cache fails closed. A call whose caller the scope cannot identify is not
cached at all, and a warning (`query_cache_skipped`) names the handler once:
caching it would share one entry among every caller the cache cannot tell
apart. That covers anonymous requests, message consumers and background jobs,
a `USER` call whose context names only a tenant, and callers whose identity
lives somewhere the cache cannot see: a session-authenticated principal kept
only on `request.state`, or a tenant kept in an application `ContextVar` (the
tenant-GUC pattern of `pyfly.data.relational.dialect_customizers`). Pass that
identity in the `ExecutionContext` of `query_with_context()`, or declare
`GLOBAL` for data that really is the same for everyone.

A tenant seen only in `X-Tenant-Id` does not make a `TENANT` entry shareable
across users: the entry is keyed by the user as well, so a client that forges
the header can neither read another tenant's entry nor poison it. If your
gateway authenticates the header, put its value in the `ExecutionContext`
(`with_tenant_id(...)`): that is the explicit statement that you trust it.

```python
from pyfly.cqrs.cache.decorators import cacheable
from pyfly.cqrs.types import QueryCacheScope

@cacheable(scope=QueryCacheScope.TENANT)
@query_handler(cacheable=True, cache_ttl=600)
class ListInvoicesHandler(QueryHandler[ListInvoicesQuery, list[InvoiceDto]]): ...
```

A `ContextAwareQueryHandler` is never served from the cache without a context:
it refuses such a call, and a hit must not bypass that.

A query key that contains `|scope=` or ends with `|generation` is never cached
(a warning names the first one): the query cache separates scopes and
generations with them, and a key built from user input could otherwise address
another caller's entry.

Evicting a key (`clear_cache`, a command's `get_cache_key()`, a bridge rule)
reaches every caller's entry with two deletes and no write, whatever the number
of tenants and users: scoped entries live under the key's current generation
(`<key>|<generation>|scope=<digest>`), and the eviction deletes the generation
(`<key>|generation`) and the key's unscoped entry. The old entries are
unreachable from then on and expire with their TTL; the next lookup starts a
fresh generation. A key that was never cached costs no write, and the deletes of
one eviction (the key under every `cache_key_prefix`) run concurrently. A scoped
lookup reads the generation first, one extra round trip.

A generation expires too: it is created with the TTL of the entries looked up
under it (`pyfly.cqrs.query.cache_ttl` when that is not known), so no
query-cache key outlives the entries it serves. A generation is never refreshed,
because rewriting it could race an eviction and bring evicted entries back: an
entry stored late in its generation's life may become unreachable before its own
TTL, which costs one extra miss and never serves a stale value.

> **Prior behavior (corrected in 26.09.08):** the key was the query's own key
> only, so the first tenant (or user) to run a cacheable query filled the entry
> every other tenant was served for `cache_ttl`, 900 seconds by default.

### Transactions, types and None

* **Writes wait for the commit.** A query run inside a unit of work stores its
  result after the commit, and not at all on rollback: it may have read a row
  that never commits.
* **A hit has the declared type.** The bus rebuilds a hit as the handler's result
  type `R`, so a Redis or PostgreSQL cache returns the DTO, not a `dict`: models
  with aliases or computed fields included (see
  [Return Types, Hits and Failures](caching.md#return-types-hits-and-failures)).
  `R` is resolved through your own generic bases and subclasses too: with
  `class PagedHandler(QueryHandler[Q, Page[T]], Generic[Q, T])`, a
  `PagedHandler[ListItemsQuery, ItemDto]` returns `Page[ItemDto]`, and a
  handler that subclasses a concrete handler inherits its query and result
  types.
  An entry that does not fit `R` is a miss, with one warning per handler. A
  result type that is an ORM-mapped class or a Beanie document is never cached
  (a `query_cache_disabled` warning names the handler once): return a DTO.
* **`None` is cached on request.** A `None` result is not stored unless the
  handler declares `@cacheable(cache_none=True)`; then it is served from the
  cache like any other result, and a lookup for a missing row stops reaching
  the database.

### Command-side invalidation

The command bus evicts what a command made stale once the command's unit of
work commits (at once when the command runs outside one, and never when it fails
or rolls back):

* the command's `get_cache_key()`, a query's cache key, for every caller and
  under every registered handler's `cache_key_prefix`;
* every cached result of the query handlers tagged with `@cache_evict(Event)`
  (from `pyfly.cqrs.cache.decorators`) for an event the command produced
  (`domain_events` on its result or on the command), or for an event its own
  handler is tagged with.

```python
from pyfly.cqrs.cache.decorators import cache_evict

@dataclass(frozen=True)
class ShipOrder(Command[None]):
    order_id: str

    def get_cache_key(self) -> str | None:
        return GetOrderQuery(order_id=self.order_id).get_cache_key()

@cache_evict(OrderUpdated)                  # its entries are stale once an OrderUpdated happens
@query_handler(cacheable=True, cache_key_prefix="orders")
class ListOrdersHandler(QueryHandler[ListOrdersQuery, list[OrderDto]]): ...

@cache_evict(OrderUpdated)                  # this command stands for an OrderUpdated
@command_handler
class RenameOrderHandler(CommandHandler[RenameOrder, None]): ...
```

An event-tag eviction removes the handler's group: `<cache_key_prefix>:`, or
`<QueryClass>:` for the default query keys. Declare a `cache_key_prefix` on a
tagged handler whose query overrides `get_cache_key()`: its keys need not start
with `<QueryClass>:`, and the first eviction that may miss them logs a
`query_cache_eviction_may_miss` warning naming the handler.

A group eviction scans the query cache's keys, once per tagged handler: on
Redis a `SCAN` of the whole database, whose cost grows with everything the
database holds (sessions, locks, other applications' keys), not only with the
query cache. Evicting by key (`get_cache_key()`) never scans. Keep event tags
for events that are rare next to the queries they invalidate, and prefer
`get_cache_key()` on hot commands.

The invalidation comes before the publication of the command's domain events,
and runs to completion even when the task is cancelled meanwhile: a handler whose
own `@transactional` committed never leaves its old results cached because an
event then fails to publish (`EventFailureStrategy.RAISE`) or the client
disconnected. Inside a wider unit of work it waits for that unit's commit, so a
publication failure that rolls the unit back drops it.

> **Prior behavior (corrected in 26.09.08):** `Command.get_cache_key()`,
> `@cache_evict(events)`, `cache_key_prefix` and `caching_enabled` were read by
> nothing, so after a committed update the query kept returning the old result
> for `cache_ttl`.

### EDA-driven cache invalidation

When an EDA `EventPublisher` bean is present, `CqrsAutoConfiguration`
also creates an **`EdaCacheInvalidationBridge`** bean and attaches it to the
bus. The bridge evicts `QueryCacheAdapter` entries in response to domain events
arriving on the `pyfly.eda` bus.

It subscribes **one handler per registered rule**, and nothing at all while no
rule is registered — a process that invalidates nothing is not a consumer of the
bus. Registering a rule after startup subscribes it there and then, so the order
of `register()` and the framework's `subscribe()` does not matter.

> **Prior behaviour (corrected in 26.09.07):** the bridge subscribed the
> wildcard `"*"` from every process the moment a CQRS context had an EDA bus,
> whether or not any rule was registered — so a process subscribed to everything
> in order to discard it. On the Postgres bus that is not passive: the adapter
> refuses to advance the consumer group's cursor while no handler is registered,
> precisely so events published before a worker subscribes are not lost, and a
> wildcard handler that dispatches nothing defeated that guard. An API sharing a
> worker's consumer group drained the worker's queue into a no-op and its jobs
> stayed queued. On Kafka and Redis the same subscription cost a partition
> assignment and a pattern subscription for no work.

Set `pyfly.cqrs.cache.invalidation.enabled: false` to refuse the bridge outright
without disabling CQRS.

Register invalidation rules on the bridge after startup (or inject the bean):

```python
from pyfly.cqrs.cache.eda_bridge import EdaCacheInvalidationBridge

# Inject the bridge bean (None when EDA is not configured)
bridge: EdaCacheInvalidationBridge | None

if bridge:
    # Evict "order:<order_id>" whenever an "order.updated" event arrives
    bridge.register("order.updated", "order:{order_id}")
    # Evict "customer:<customer_id>" on "customer.profile-changed"
    bridge.register("customer.profile-changed", "customer:{customer_id}")
```

**How rules work:**

- `event_type` is matched against the `event_type` field of the incoming
  `EventEnvelope`.
- `cache_key_pattern` may contain `{field}` placeholders that are resolved
  from the envelope's `payload` dict at eviction time.
- Multiple patterns can be registered for the same event type by calling
  `register()` more than once.
- Unresolvable placeholders are left as-is and a warning is logged; eviction
  still proceeds for resolvable keys.

The full prefixed cache key evicted is `:cqrs:<resolved_pattern>` (the
`QueryCacheAdapter` applies the `:cqrs:` prefix transparently), for every
caller's scope. A pattern must resolve to the key the query bus stores: a query
that uses the default key (`<QueryClass>:<digest>`) needs `get_cache_key()`
overridden (for example to `order:{order_id}`) for a payload-field rule to match
it, and the entries of a handler that declares a `cache_key_prefix` live under
`<cache_key_prefix>:<query key>`, so its rules include the prefix
(`orders:order:{order_id}`). Inside a unit of work the eviction waits for the
commit.

> **Prior behaviour (corrected):** Before SP-8 the `QueryCacheAdapter` never
> received a real `CacheAdapter` at startup, so `@cacheable` queries were
> silently never cached. The EDA-driven invalidation bridge existed in source but
> was never wired. Both are now fully operational when the respective beans are
> present.

---

## Domain Events

The `DefaultCommandBus` publishes domain events after handler execution by
checking the result and command for a `domain_events` attribute.

```python
from pyfly.cqrs.event.publisher import CommandEventPublisher, NoOpEventPublisher, EdaCommandEventPublisher
```

| Class | Description |
|-------|-------------|
| `CommandEventPublisher` | Protocol: `async def publish(event, *, destination=None)`. |
| `NoOpEventPublisher` | Silent no-op (default when no EDA is configured). |
| `EdaCommandEventPublisher` | Delegates to pyfly's EDA `EventPublisher` port. |

```python
from pyfly.cqrs.event.publisher import EdaCommandEventPublisher
publisher = EdaCommandEventPublisher(producer=eda_publisher, default_destination="cqrs.events")
bus = DefaultCommandBus(registry=registry, event_publisher=publisher)
```

`EdaCommandEventPublisher` derives the `event_type` from the event's
`event_type` attribute when present, otherwise falls back to the class name.
The payload is serialized via `dataclasses.asdict` for dataclass events, or
`__dict__` for plain objects.

### @publish_domain_event decorator

Apply `@publish_domain_event` to a command handler class to control which
destination the bus uses when publishing that handler's domain events:

```python
from pyfly.cqrs.event.decorators import publish_domain_event
from pyfly.cqrs.decorators import command_handler

@publish_domain_event(destination="orders.events")
@command_handler
class CreateOrderHandler(CommandHandler[CreateOrderCommand, OrderId]):
    async def do_handle(self, command: CreateOrderCommand) -> OrderId:
        ...
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `destination` | `str \| None` | `None` | Target topic/queue. `None` uses the publisher's default (`cqrs.events`). |
| `message_format` | `str` | `"json"` | Message format (`"json"` or `"avro"`). |

The decorator sets `__pyfly_publish_event__ = True` and
`__pyfly_event_destination__` on the handler class. The `DefaultCommandBus`
reads `__pyfly_event_destination__` at event-publish time and passes it as the
`destination` keyword argument to `CommandEventPublisher.publish()`.

> **SP-8 change:** `@publish_domain_event(destination=...)` was previously
> parsed but never read by the command bus; event publishing always fell back
> to the publisher's default destination. The bus now honours the decorator's
> `destination` value.

---

## Fluent Builders

### CommandBuilder

```python
from pyfly.cqrs.fluent.command_builder import CommandBuilder

result = await (
    CommandBuilder.create(CreateOrderCommand)
    .with_field("customer_id", "cust-1")
    .with_field("items", ["A"])
    .with_field("total", 29.99)
    .correlated_by("req-abc")
    .initiated_by("user-42")
    .execute_with(command_bus)
)
```

Methods: `create(type)`, `with_field(name, value)`, `with_fields(**kw)`,
`correlated_by(id)`, `initiated_by(user_id)`, `at(timestamp)`,
`with_metadata(key, value)`, `build()`, `execute_with(bus)`.

### QueryBuilder

```python
from pyfly.cqrs.fluent.query_builder import QueryBuilder

order = await (
    QueryBuilder.create(GetOrderQuery)
    .with_field("order_id", "ord-123")
    .cached(True)
    .execute_with(query_bus)
)
```

Methods: `create(type)`, `with_field(name, value)`, `with_fields(**kw)`,
`correlated_by(id)`, `at(timestamp)`, `with_metadata(key, value)`,
`cached(enabled)`, `with_cache_key(key)`, `build()`, `execute_with(bus)`.

---

## Configuration Reference

```yaml
pyfly:
  cqrs:
    enabled: true
    command:
      timeout: 30
      metrics_enabled: true
      tracing_enabled: true
    query:
      timeout: 15
      caching_enabled: true
      cache_ttl: 900
      metrics_enabled: true
      tracing_enabled: true
    authorization:
      enabled: true
      custom:
        enabled: true
        timeout_ms: 5000
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.cqrs.enabled` | `bool` | `true` | Master switch. |
| `pyfly.cqrs.command.timeout` | `int` | `30` | Command timeout (seconds). |
| `pyfly.cqrs.command.metrics_enabled` | `bool` | `true` | Command metrics. |
| `pyfly.cqrs.command.tracing_enabled` | `bool` | `true` | Command tracing. |
| `pyfly.cqrs.query.timeout` | `int` | `15` | Query timeout (seconds). |
| `pyfly.cqrs.query.caching_enabled` | `bool` | `true` | Query caching; `false` turns the query cache off. |
| `pyfly.cqrs.query.cache_ttl` | `int` | `900` | Default cache TTL (seconds). |
| `pyfly.cqrs.query.metrics_enabled` | `bool` | `true` | Query metrics. |
| `pyfly.cqrs.query.tracing_enabled` | `bool` | `true` | Query tracing. |
| `pyfly.cqrs.cache.invalidation.enabled` | `bool` | `true` | Wire the EDA-driven cache-invalidation bridge. `false` refuses the bean without disabling CQRS. |
| `pyfly.cqrs.authorization.enabled` | `bool` | `true` | Authorization checks. |
| `pyfly.cqrs.authorization.custom.enabled` | `bool` | `true` | Custom authorization. |
| `pyfly.cqrs.authorization.custom.timeout_ms` | `int` | `5000` | Custom auth timeout. |

Properties are bound via `@config_properties(prefix="pyfly.cqrs")` to `CqrsProperties`.

---

## Auto-Configuration

`CqrsAutoConfiguration` activates when `pyfly.cqrs.enabled=true` and wires
these beans into the DI container:

| Bean | Type | Notes |
|------|------|-------|
| `cqrs_properties` | `CqrsProperties` | |
| `correlation_context` | `CorrelationContext` | |
| `auto_validation_processor` | `AutoValidationProcessor` | |
| `command_validation_service` | `CommandValidationService` | |
| `cqrs_metrics_service` | `CqrsMetricsService` | Optionally injects `MetricsRegistry` |
| `authorization_service` | `AuthorizationService` | |
| `handler_registry` | `HandlerRegistry` | |
| `command_event_publisher` | `CommandEventPublisher` | `EdaCommandEventPublisher` when an EDA `EventPublisher` bean is present; `NoOpEventPublisher` otherwise |
| `command_bus` | `DefaultCommandBus` | |
| `query_cache_adapter` | `QueryCacheAdapter` | Injects `CacheAdapter` when available; no-op otherwise |
| `eda_cache_invalidation_bridge` | `EdaCacheInvalidationBridge \| None` | Created and attached to the EDA bus when an `EventPublisher` bean is present, subscribing one event type per registered rule; `None` otherwise. Gated by `pyfly.cqrs.cache.invalidation.enabled` (default `true`) |
| `query_bus` | `DefaultQueryBus` | |

`cqrs_metrics_service` optionally injects a `MetricsRegistry` bean from the
observability module; when no registry is present all recording methods are
silent no-ops.

> **Key Point:** Inject `CommandBus` or `QueryBus` by type. The DI
> container resolves all dependencies automatically.

---

## Actuator Endpoints

### CqrsMetricsEndpoint

Exposes handler counts at `/actuator/cqrs/metrics`.

```python
from pyfly.cqrs.actuator.endpoint import CqrsMetricsEndpoint
endpoint = CqrsMetricsEndpoint(registry=handler_registry)
endpoint.get_metrics()
# {"command_handlers": 3, "query_handlers": 2, "registered_command_types": [...], ...}
```

### CqrsHealthIndicator

Reports `UP` when at least one handler is registered, `UNKNOWN` otherwise.

```python
from pyfly.cqrs.actuator.health import CqrsHealthIndicator
indicator = CqrsHealthIndicator(registry=handler_registry)
indicator.health()
# {"status": "UP", "details": {"command_handlers": 3, "query_handlers": 2}}
```

---

## Complete Example: Order Management

```python
from dataclasses import dataclass
from pyfly.container import service
from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import command_handler, query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Command, Query
from pyfly.cqrs.validation.types import ValidationResult

# -- Messages --

@dataclass(frozen=True)
class CreateOrderCommand(Command[str]):
    customer_id: str
    items: list[str]
    total: float

    async def validate(self) -> ValidationResult:
        if self.total <= 0:
            return ValidationResult.failure("total", "Must be positive")
        return ValidationResult.success()

@dataclass(frozen=True)
class CancelOrderCommand(Command[bool]):
    order_id: str
    reason: str

    async def authorize(self) -> AuthorizationResult:
        if not self.reason:
            return AuthorizationResult.failure("order", "Reason required")
        return AuthorizationResult.success()

@dataclass(frozen=True)
class GetOrderQuery(Query[dict | None]):
    order_id: str

# -- Handlers --

@command_handler
@service
class CreateOrderHandler(CommandHandler[CreateOrderCommand, str]):
    async def do_handle(self, command: CreateOrderCommand) -> str:
        return "ord-new-123"

@command_handler
@service
class CancelOrderHandler(CommandHandler[CancelOrderCommand, bool]):
    async def do_handle(self, command: CancelOrderCommand) -> bool:
        return True

@query_handler(cacheable=True, cache_ttl=300)
@service
class GetOrderHandler(QueryHandler[GetOrderQuery, dict | None]):
    async def do_handle(self, query: GetOrderQuery) -> dict | None:
        return {"order_id": query.order_id, "status": "ACTIVE"}

# -- Wiring --

registry = HandlerRegistry()
registry.register_command_handler(CreateOrderHandler())
registry.register_command_handler(CancelOrderHandler())
registry.register_query_handler(GetOrderHandler())

command_bus = DefaultCommandBus(registry=registry)
query_bus = DefaultQueryBus(registry=registry)

# -- Usage --

async def main() -> None:
    order_id = await command_bus.send(
        CreateOrderCommand(customer_id="cust-42", items=["widget"], total=19.99)
    )
    ctx = ExecutionContextBuilder().with_user_id("cust-42").with_tenant_id("acme").build()
    order = await query_bus.query_with_context(GetOrderQuery(order_id=order_id), ctx)
    await command_bus.send(CancelOrderCommand(order_id=order_id, reason="Changed my mind"))
```

---

## Testing CQRS Components

### Handler Isolation

```python
import pytest

@pytest.mark.asyncio
async def test_create_order_handler() -> None:
    handler = CreateOrderHandler()
    result = await handler.do_handle(
        CreateOrderCommand(customer_id="test", items=["A"], total=9.99)
    )
    assert isinstance(result, str)
```

### Full Pipeline

```python
import pytest
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.registry import HandlerRegistry

@pytest.fixture
def command_bus() -> DefaultCommandBus:
    registry = HandlerRegistry()
    registry.register_command_handler(CreateOrderHandler())
    return DefaultCommandBus(registry=registry)

@pytest.mark.asyncio
async def test_send_through_bus(command_bus: DefaultCommandBus) -> None:
    result = await command_bus.send(
        CreateOrderCommand(customer_id="test", items=["A"], total=9.99)
    )
    assert result is not None
```

### Validation and Authorization

```python
@pytest.mark.asyncio
async def test_validation_rejects_zero_total() -> None:
    result = await CreateOrderCommand(customer_id="t", items=["A"], total=0).validate()
    assert not result.valid

@pytest.mark.asyncio
async def test_cancel_without_reason_denied() -> None:
    result = await CancelOrderCommand(order_id="o", reason="").authorize()
    assert not result.authorized
```

> **Key Point:** `DefaultCommandBus` and `DefaultQueryBus` are plain
> Python classes with no global state, so you can construct them freely in
> test fixtures without mocking.
