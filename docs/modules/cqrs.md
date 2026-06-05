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
| `get_cache_key()` | `None` | Cache-invalidation key. |

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
caching metadata methods: `supports_caching()` and `get_cache_ttl_seconds()`.

```python
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.decorators import query_handler
from pyfly.container import service

@query_handler(cacheable=True, cache_ttl=300)
@service
class GetOrderHandler(QueryHandler[GetOrderQuery, dict | None]):
    def __init__(self, repo: OrderRepository) -> None:
        self._repo = repo

    async def do_handle(self, query: GetOrderQuery) -> dict | None:
        return await self._repo.find_by_id(query.order_id)
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
| `cache_key_prefix` | `str \| None` | `None` | Key prefix. |
| `priority` | `int` | `0` | Lower = higher priority. |
| `tags` | `tuple[str, ...]` | `()` | Arbitrary tags. |
| `description` | `str` | `""` | Description. |

---

## CommandBus

`CommandBus` is a `@runtime_checkable Protocol` with `send()`,
`send_with_context()`, `register_handler()`, `unregister_handler()`, and
`has_handler()` methods.

### DefaultCommandBus

Pipeline: correlate, validate, authorize, execute, publish events, record metrics.

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

`QueryCacheAdapter` bridges pyfly's cache module with CQRS, prefixing all
keys with `:cqrs:`. Without an underlying cache, operations are silent no-ops.

```python
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
adapter = QueryCacheAdapter(cache=my_cache_instance)
```

| Method | Description |
|--------|-------------|
| `get(key)` | Fetch cached value. |
| `put(key, value, ttl)` | Store with optional `timedelta` TTL. |
| `evict(key)` | Remove a key. |
| `clear()` | Remove all entries. |
| `is_available` | Whether cache is configured. |

Enable caching: `@query_handler(cacheable=True, cache_ttl=600)`. The query
must have `is_cacheable()` return `True` (the default). Invalidate via
`await query_bus.clear_cache("key")` or `await query_bus.clear_all_cache()`.

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
| `EdaCommandEventPublisher` | Delegates to pyfly's messaging `Producer`. |

```python
from pyfly.cqrs.event.publisher import EdaCommandEventPublisher
publisher = EdaCommandEventPublisher(producer=kafka_producer, default_destination="cqrs.events")
bus = DefaultCommandBus(registry=registry, event_publisher=publisher)
```

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
| `pyfly.cqrs.query.caching_enabled` | `bool` | `true` | Query caching. |
| `pyfly.cqrs.query.cache_ttl` | `int` | `900` | Default cache TTL (seconds). |
| `pyfly.cqrs.query.metrics_enabled` | `bool` | `true` | Query metrics. |
| `pyfly.cqrs.query.tracing_enabled` | `bool` | `true` | Query tracing. |
| `pyfly.cqrs.authorization.enabled` | `bool` | `true` | Authorization checks. |
| `pyfly.cqrs.authorization.custom.enabled` | `bool` | `true` | Custom authorization. |
| `pyfly.cqrs.authorization.custom.timeout_ms` | `int` | `5000` | Custom auth timeout. |

Properties are bound via `@config_properties(prefix="pyfly.cqrs")` to `CqrsProperties`.

---

## Auto-Configuration

`CqrsAutoConfiguration` activates when `pyfly.cqrs.enabled=true` and wires
these beans into the DI container:

| Bean | Type |
|------|------|
| `cqrs_properties` | `CqrsProperties` |
| `correlation_context` | `CorrelationContext` |
| `auto_validation_processor` | `AutoValidationProcessor` |
| `command_validation_service` | `CommandValidationService` |
| `cqrs_metrics_service` | `CqrsMetricsService` |
| `authorization_service` | `AuthorizationService` |
| `handler_registry` | `HandlerRegistry` |
| `command_bus` | `DefaultCommandBus` |
| `query_cache_adapter` | `QueryCacheAdapter` |
| `query_bus` | `DefaultQueryBus` |

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
