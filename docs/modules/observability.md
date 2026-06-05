# Observability Guide

Production applications need visibility into their runtime behavior. PyFly provides
first-class support for the three pillars of observability -- metrics, tracing, and
logging -- along with a health check system for readiness and liveness probes.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Metrics](#metrics)
   - [MetricsRegistry](#metricsregistry)
   - [Counter Metrics](#counter-metrics)
   - [Histogram Metrics](#histogram-metrics)
   - [@timed Decorator](#timed-decorator)
   - [@counted Decorator](#counted-decorator)
   - [Prometheus Integration](#prometheus-integration)
3. [Tracing](#tracing)
   - [@span Decorator](#span-decorator)
   - [Error Recording](#error-recording)
   - [OpenTelemetry Integration](#opentelemetry-integration)
4. [Logging](#logging)
   - [Quick Start with configure_logging and get_logger](#quick-start-with-configure_logging-and-get_logger)
   - [LoggingPort Protocol](#loggingport-protocol)
   - [StructlogAdapter](#structlogadapter)
   - [Structured Logging with Key-Value Pairs](#structured-logging-with-key-value-pairs)
   - [Correlation IDs](#correlation-ids)
5. [Health Checks](#health-checks)
   - [HealthChecker](#healthchecker)
   - [HealthStatus Enum](#healthstatus-enum)
   - [HealthResult Dataclass](#healthresult-dataclass)
6. [Auto-Configuration](#auto-configuration)
7. [Configuration](#configuration)
   - [Logging Settings](#logging-settings)
   - [Metrics and Actuator Settings](#metrics-and-actuator-settings)
8. [Complete Example](#complete-example)

---

## Introduction

Observability answers three fundamental questions about a running system:

| Pillar   | Question                                  | PyFly Module                       |
|----------|-------------------------------------------|------------------------------------|
| Metrics  | "How much?" / "How fast?"                 | `pyfly.observability.metrics`      |
| Tracing  | "What path did this request take?"        | `pyfly.observability.tracing`      |
| Logging  | "What happened, and in what context?"     | `pyfly.logging`                    |

PyFly also provides **health checks** (`pyfly.actuator.health`) so orchestrators
like Kubernetes can determine whether a service is ready to receive traffic.

All observability utilities are importable from a single package:

```python
from pyfly.observability import (
    MetricsRegistry, timed, counted,   # Metrics
    span,                               # Tracing
    configure_logging, get_logger,      # Logging
    HealthChecker, HealthResult, HealthStatus,  # Health
)
```

---

## Metrics

### MetricsRegistry

`MetricsRegistry` is a thin wrapper around the `prometheus_client` library. It
provides a clean API for creating counters and histograms, and it guarantees that
each metric name is registered only once -- duplicate calls to `counter()` or
`histogram()` with the same name return the existing metric rather than raising an
error.

```python
from pyfly.observability import MetricsRegistry

registry = MetricsRegistry()
```

Internally the registry maintains two dictionaries:

```python
self._counters: dict[str, Counter] = {}
self._histograms: dict[str, Histogram] = {}
```

**Source:** `src/pyfly/observability/metrics.py`

### Counter Metrics

A counter is a monotonically increasing value. Use it to count events such as
requests handled, errors raised, or items processed.

```python
# Create (or retrieve) a counter
requests_total = registry.counter(
    name="http_requests_total",
    description="Total HTTP requests received",
    labels=["method", "path"],
)

# Increment without labels
requests_total.inc()

# Increment with labels
requests_total.labels(method="GET", path="/orders").inc()
```

**`counter()` Parameters:**

| Parameter     | Type              | Default | Description                          |
|---------------|-------------------|---------|--------------------------------------|
| `name`        | `str`             | required | Prometheus metric name              |
| `description` | `str`             | required | Human-readable description          |
| `labels`      | `list[str] \| None` | `None` | Label names for multi-dimensional metrics |

The returned object is a standard `prometheus_client.Counter`. All methods from that
class (`inc()`, `labels()`, etc.) are available.

### Histogram Metrics

A histogram samples observations (usually durations or sizes) and counts them in
configurable buckets. It is the foundation for percentile calculations and SLA
monitoring.

```python
# Create a histogram with custom buckets
request_duration = registry.histogram(
    name="http_request_duration_seconds",
    description="HTTP request processing time",
    labels=["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Record an observation
request_duration.labels(method="GET", path="/orders").observe(0.042)
```

**`histogram()` Parameters:**

| Parameter     | Type                    | Default | Description                     |
|---------------|-------------------------|---------|---------------------------------|
| `name`        | `str`                   | required | Prometheus metric name         |
| `description` | `str`                   | required | Human-readable description     |
| `labels`      | `list[str] \| None`       | `None` | Label names                    |
| `buckets`     | `tuple[float, ...] \| None` | `None` | Custom histogram buckets. Uses Prometheus defaults when `None`. |

The returned object is a standard `prometheus_client.Histogram`.

### @timed Decorator

The `@timed` decorator records the execution duration of an **async** function as a
histogram observation. It wraps the function with `time.perf_counter()` calls and
records the elapsed time even when the function raises an exception.

```python
from pyfly.observability import MetricsRegistry, timed

registry = MetricsRegistry()

@timed(registry, "order_processing_seconds", "Time to process an order")
async def process_order(order_id: str) -> dict:
    # ... business logic ...
    return {"order_id": order_id, "status": "processed"}
```

**How it works internally:**

1. Before calling the decorated function, records `start = time.perf_counter()`.
2. Awaits the decorated function inside a `try/finally` block.
3. In the `finally` clause, observes `time.perf_counter() - start` on the histogram.

This means the duration is recorded regardless of whether the function succeeds or
raises an exception. The actual source implementation:

```python
@functools.wraps(func)
async def wrapper(*args: Any, **kwargs: Any) -> Any:
    start = time.perf_counter()
    try:
        return await func(*args, **kwargs)
    finally:
        histogram.observe(time.perf_counter() - start)
```

**Parameters:**

| Parameter     | Type              | Description                               |
|---------------|-------------------|-------------------------------------------|
| `registry`    | `MetricsRegistry` | The registry that owns the histogram      |
| `name`        | `str`             | Histogram metric name                     |
| `description` | `str`             | Human-readable description                |

### @counted Decorator

The `@counted` decorator increments a counter each time an **async** function is
invoked.

```python
from pyfly.observability import MetricsRegistry, counted

registry = MetricsRegistry()

@counted(registry, "orders_created_total", "Total orders created")
async def create_order(data: dict) -> dict:
    # ... business logic ...
    return {"id": "ord-123", **data}
```

**How it works internally:**

1. Before calling the decorated function, increments the counter with `counter.inc()`.
2. Awaits the decorated function normally.

The counter increments before the function executes, so the count increases even if
the function subsequently raises an exception.

```python
@functools.wraps(func)
async def wrapper(*args: Any, **kwargs: Any) -> Any:
    counter.inc()
    return await func(*args, **kwargs)
```

**Parameters:**

| Parameter     | Type              | Description                               |
|---------------|-------------------|-------------------------------------------|
| `registry`    | `MetricsRegistry` | The registry that owns the counter        |
| `name`        | `str`             | Counter metric name                       |
| `description` | `str`             | Human-readable description                |

### Combining @timed and @counted

You can stack both decorators on the same function:

```python
@timed(registry, "order_duration_seconds", "Order processing time")
@counted(registry, "orders_total", "Total orders processed")
async def process_order(order_id: str) -> dict:
    ...
```

The decorators execute from bottom to top: `@counted` runs first (increments the
counter), then `@timed` wraps the whole call (records the duration).

### Prometheus Integration

PyFly metrics are built directly on top of `prometheus_client`. This means you can
expose them through the standard Prometheus HTTP handler or through the PyFly actuator
metrics endpoint.

```python
# Expose metrics for Prometheus scraping
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

async def metrics_endpoint(request):
    """Expose Prometheus metrics for scraping."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
```

Since `MetricsRegistry` returns native `prometheus_client` objects, all existing
Prometheus ecosystem tools (Grafana dashboards, alerting rules, recording rules)
work without modification.

---

## Tracing

### @span Decorator

The `@span` decorator wraps an **async** function in an OpenTelemetry span. This
enables distributed tracing across service boundaries -- each span records the
function's name, timing, and any errors that occur.

```python
from pyfly.observability import span

@span("fetch-inventory")
async def fetch_inventory(sku: str) -> dict:
    # ... call inventory service ...
    return {"sku": sku, "quantity": 42}
```

**Parameters:**

| Parameter | Type  | Description                              |
|-----------|-------|------------------------------------------|
| `name`    | `str` | The name of the span in the trace viewer |

Under the hood, PyFly creates a tracer named `"pyfly"`:

```python
from opentelemetry import trace

_tracer = trace.get_tracer("pyfly")
```

**Source:** `src/pyfly/observability/tracing.py`

### Error Recording

When the decorated function raises an exception, the span automatically:

1. Sets the span status to `ERROR` with the exception message via
   `trace.Status(trace.StatusCode.ERROR, str(exc))`.
2. Records the exception on the span via `current_span.record_exception(exc)`.
3. Re-raises the original exception so callers see it unmodified.

```python
@span("risky-operation")
async def risky_operation() -> None:
    raise ValueError("something went wrong")

# The exception propagates normally, but the span records:
# - status: ERROR
# - exception type, message, and traceback
```

The full wrapper implementation:

```python
@functools.wraps(func)
async def wrapper(*args: Any, **kwargs: Any) -> Any:
    with _tracer.start_as_current_span(name) as current_span:
        try:
            result = await func(*args, **kwargs)
            return result
        except Exception as exc:
            current_span.set_status(
                trace.Status(trace.StatusCode.ERROR, str(exc))
            )
            current_span.record_exception(exc)
            raise
```

### OpenTelemetry Integration

When the `opentelemetry` libraries are installed, `TracingAutoConfiguration`
builds the global `TracerProvider` **and** attaches a `BatchSpanProcessor` with
an exporter for you, so `@span` traces are actually exported. (Previously the
auto-configured provider had no span processor, so every span was recorded and
immediately discarded.)

Select the exporter through configuration:

```yaml
pyfly:
  observability:
    tracing:
      exporter: otlp          # otlp | console | none
      otlp:
        endpoint: "http://localhost:4318"   # OTLP/HTTP endpoint
```

Exporter selection rules (see `TracingAutoConfiguration._install_span_processor`):

- `pyfly.observability.tracing.exporter` chooses `otlp`, `console`, or `none`
  explicitly.
- When `exporter` is unset, OTLP is auto-selected **iff** an endpoint is
  configured — either `pyfly.observability.tracing.otlp.endpoint` or the
  standard `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable. Otherwise no
  exporter is wired and a single info line is logged so the drop is not silent.
- `console` uses the OpenTelemetry `ConsoleSpanExporter`.
- `otlp` requires `opentelemetry-exporter-otlp` (OTLP/HTTP); if it is not
  installed a warning is logged and spans are dropped.

You can still configure the SDK yourself instead — for example to use a gRPC
exporter — by registering your own `TracerProvider`:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

# Configure the tracer provider
provider = TracerProvider()
processor = BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4317"))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

# Now all @span decorators automatically export to OTLP
```

### Nesting Spans

Spans nest automatically through OpenTelemetry's context propagation:

```python
@span("process-order")
async def process_order(order_id: str) -> dict:
    customer = await fetch_customer(order_id)   # child span
    inventory = await check_inventory(order_id)  # child span
    return {"customer": customer, "inventory": inventory}

@span("fetch-customer")
async def fetch_customer(order_id: str) -> dict:
    ...

@span("check-inventory")
async def check_inventory(order_id: str) -> dict:
    ...
```

In a trace viewer, this appears as:

```
process-order [200ms]
  +-- fetch-customer [50ms]
  +-- check-inventory [30ms]
```

---

## Logging

PyFly provides two complementary logging APIs:

1. **`pyfly.logging`** -- hexagonal architecture with `LoggingPort` (protocol) and
   `StructlogAdapter` (default implementation) for framework-level integration.

Both are backed by [structlog](https://www.structlog.org/) for structured, key-value
logging.

### Quick Start with configure_logging and get_logger

For simple applications, call `configure_logging()` once at startup and use
`get_logger()` to obtain named loggers:

```python
from pyfly.observability import configure_logging, get_logger

# Configure once at startup
configure_logging(level="DEBUG", json_output=False)

# Get a named logger anywhere in your code
logger = get_logger("order_service")

logger.info("order_created", order_id="ord-123", customer="acme")
logger.warning("inventory_low", sku="WIDGET-42", remaining=3)
logger.error("payment_failed", order_id="ord-123", reason="declined")
```

**`configure_logging()` Parameters:**

| Parameter    | Type   | Default  | Description                                   |
|-------------|--------|----------|-----------------------------------------------|
| `level`     | `str`  | `"INFO"` | Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `json_output` | `bool` | `False` | `True` for JSON lines (production), `False` for colored console (development) |

**Processors configured automatically:**

| Processor                            | Purpose                                    |
|--------------------------------------|--------------------------------------------|
| `merge_contextvars`                  | Merges context variables (correlation IDs)  |
| `add_logger_name`                    | Adds the logger name to each event          |
| `add_log_level`                      | Adds the log level                          |
| `TimeStamper(fmt="iso")`             | ISO 8601 timestamps                         |
| `StackInfoRenderer`                  | Includes stack traces on errors             |
| `UnicodeDecoder`                     | Decodes byte strings                        |
| `ConsoleRenderer` or `JSONRenderer`  | Based on `json_output` flag                 |

**`get_logger()` Parameters:**

| Parameter | Type  | Description              |
|-----------|-------|--------------------------|
| `name`    | `str` | The logger name          |

Returns a `structlog.stdlib.BoundLogger`.

**Source:** `src/pyfly/observability/logging.py`

### LoggingPort Protocol

For applications following hexagonal architecture, PyFly defines a `LoggingPort`
protocol so the logging implementation can be swapped without changing application
code.

```python
from pyfly.logging import LoggingPort

# LoggingPort is a runtime-checkable Protocol with three methods:
@runtime_checkable
class LoggingPort(Protocol):
    def configure(self, config: Config) -> None: ...
    def get_logger(self, name: str) -> Any: ...
    def set_level(self, name: str, level: str) -> None: ...
```

**Methods:**

| Method       | Parameters                    | Description                           |
|-------------|-------------------------------|---------------------------------------|
| `configure` | `config: Config`              | Configure logging from application config |
| `get_logger`| `name: str`                   | Get a logger by name                  |
| `set_level` | `name: str, level: str`       | Set the log level for a specific logger |

Because `LoggingPort` is a `runtime_checkable` Protocol, you can check whether an
object satisfies it with `isinstance()`:

```python
adapter = StructlogAdapter()
assert isinstance(adapter, LoggingPort)  # True
```

**Source:** `src/pyfly/logging/port.py`

### StructlogAdapter

`StructlogAdapter` is the default `LoggingPort` implementation. PyFly uses it
automatically during application bootstrap in `PyFlyApplication.__init__()`.

```python
from pyfly.logging import StructlogAdapter
from pyfly.core.config import Config

adapter = StructlogAdapter()
adapter.configure(config)

logger = adapter.get_logger("my_module")
logger.info("starting", component="scheduler")

# Change log level at runtime
adapter.set_level("sqlalchemy.engine", "WARNING")
```

**Configuration keys read from `pyfly.yaml`:**

| Config Key                     | Description                        | Default    |
|-------------------------------|------------------------------------|------------|
| `pyfly.logging.level.root`    | Root log level                     | `"INFO"`   |
| `pyfly.logging.level.<module>` | Per-module log level override     | (inherits root) |
| `pyfly.logging.format`        | Output format: `"console"` or `"json"` | `"console"` |

When `configure()` is called, the adapter performs these steps:

1. Reads the `pyfly.logging.level` section from config.
2. Extracts the `root` level and collects per-module overrides.
3. Reads `pyfly.logging.format` to determine the output renderer.
4. Configures structlog processors (same set as `configure_logging()`).
5. Sets up `logging.basicConfig` with the root level and `force=True`.
6. Applies per-module levels via `logging.getLogger(module).setLevel()`.

**Source:** `src/pyfly/logging/structlog_adapter.py`

### Structured Logging with Key-Value Pairs

Structured logging replaces format-string interpolation with explicit key-value pairs.
This makes logs machine-parseable while remaining human-readable.

```python
logger = get_logger("payment_service")

# Structured key-value pairs -- each becomes a field in JSON output
logger.info("payment_processed",
    order_id="ord-456",
    amount=99.99,
    currency="USD",
    gateway="stripe",
)
```

**Console output** (development with `json_output=False`):

```
2026-01-15T10:30:00Z [info    ] payment_processed  order_id=ord-456 amount=99.99 currency=USD gateway=stripe
```

**JSON output** (production with `json_output=True`):

```json
{"event": "payment_processed", "order_id": "ord-456", "amount": 99.99, "currency": "USD", "gateway": "stripe", "timestamp": "2026-01-15T10:30:00Z", "level": "info", "logger": "payment_service"}
```

### Correlation IDs

Use structlog's context variables to propagate correlation IDs across async call
chains. The `merge_contextvars` processor (configured automatically) includes these
variables in every log entry within the same async context.

```python
import structlog

# Bind a correlation ID to the current context (e.g., in middleware)
structlog.contextvars.bind_contextvars(
    correlation_id="req-abc-123",
    user_id="user-42",
)

# All subsequent log calls in this async context include these fields
logger.info("processing_request")
# Output includes: correlation_id=req-abc-123 user_id=user-42

logger.info("fetching_data", table="orders")
# Output includes: correlation_id=req-abc-123 user_id=user-42 table=orders

# Clear context when the request completes
structlog.contextvars.unbind_contextvars("correlation_id", "user_id")
```

PyFly's `TransactionIdMiddleware` (part of the web layer) automatically sets a
transaction ID on each incoming HTTP request, making it available in all logs for
that request's lifecycle.

---

## Health Checks

### HealthChecker

`HealthChecker` aggregates health checks from multiple components (database, cache,
message broker, external services). Register async check functions that return
`True` (healthy) or `False` (unhealthy), then call `check()` to get an aggregated
result.

```python
from pyfly.observability import HealthChecker

checker = HealthChecker()

# Register health checks -- each is an async function returning bool
async def db_health() -> bool:
    try:
        await database.execute("SELECT 1")
        return True
    except Exception:
        return False

async def redis_health() -> bool:
    try:
        return await redis_client.ping()
    except Exception:
        return False

checker.add_check("database", db_health)
checker.add_check("redis", redis_health)

# Run all checks
result = await checker.check()
print(result.status)   # HealthStatus.UP or HealthStatus.DOWN
print(result.checks)   # {"database": HealthStatus.UP, "redis": HealthStatus.DOWN}
```

**`add_check()` Parameters:**

| Parameter | Type                              | Description                          |
|-----------|-----------------------------------|--------------------------------------|
| `name`    | `str`                             | Identifier for this health check     |
| `check`   | `Callable[[], Awaitable[bool]]`   | Async function returning True/False  |

**`check()` Return:**

Returns a `HealthResult` dataclass. The overall status is `UP` only when **all**
individual checks pass. If any check returns `False` or raises an exception, the
overall status is `DOWN`.

**Exception handling:** If a check function raises an exception instead of returning
`False`, the `HealthChecker` catches it and treats that component as `DOWN`. This
prevents a single broken check from crashing the entire health check system.

**Source:** `src/pyfly/observability/health.py`

### HealthStatus Enum

```python
from pyfly.observability import HealthStatus

class HealthStatus(Enum):
    UP = "UP"
    DOWN = "DOWN"
```

| Value  | Meaning                              |
|--------|--------------------------------------|
| `UP`   | Component is healthy and operational |
| `DOWN` | Component is unhealthy or unreachable |

### HealthResult Dataclass

```python
from pyfly.observability import HealthResult

@dataclass
class HealthResult:
    status: HealthStatus
    checks: dict[str, HealthStatus] = field(default_factory=dict)
```

| Field    | Type                          | Description                          |
|----------|-------------------------------|--------------------------------------|
| `status` | `HealthStatus`                | Overall aggregated health status     |
| `checks` | `dict[str, HealthStatus]`     | Per-component health status map      |

Note that this is the observability module's `HealthResult`. The actuator module has
its own `HealthResult` with additional fields like `components` and a `to_dict()`
method. See the [Actuator Guide](actuator.md) for the production-oriented health
check system.

---

## Auto-Configuration

PyFly auto-configures observability infrastructure when the required libraries are installed. No manual bean registration is needed.

### MetricsAutoConfiguration

**Conditions:** `prometheus_client` library installed.

| Bean | Type | Description |
|------|------|-------------|
| `metrics_registry` | `MetricsRegistry` | Singleton registry for creating counters and histograms |

```yaml
pyfly:
  observability:
    metrics:
      enabled: true   # Default: true
```

With auto-configuration, you can inject `MetricsRegistry` directly into your services:

```python
@service
class OrderService:
    def __init__(self, registry: MetricsRegistry) -> None:
        self._counter = registry.counter("orders_total", "Total orders placed")
```

### TracingAutoConfiguration

**Conditions:** `opentelemetry` libraries installed (`opentelemetry-api`, `opentelemetry-sdk`).

| Bean | Type | Config Keys |
|------|------|-------------|
| `tracer_provider` | `TracerProvider` | `pyfly.observability.tracing.service-name`, `pyfly.observability.tracing.exporter`, `pyfly.observability.tracing.otlp.endpoint` |

```yaml
pyfly:
  observability:
    tracing:
      enabled: true                        # Default: true
      service-name: "${pyfly.app.name}"    # Inherits app name by default
      exporter: otlp                       # otlp | console | none
      otlp:
        endpoint: "http://localhost:4318"  # OTLP/HTTP exporter endpoint
```

The auto-configured `TracerProvider` creates an OpenTelemetry `TracerProvider` with a `Resource` containing the service name, attaches a `BatchSpanProcessor` with the configured exporter (so spans are actually exported), and sets it as the global tracer provider. See [OpenTelemetry Integration](#opentelemetry-integration) for exporter selection rules, including the `OTEL_EXPORTER_OTLP_ENDPOINT` auto-detection.

### Overriding Auto-Configured Beans

Provide your own beans via `@configuration` + `@bean` to override the auto-configured versions:

```python
from pyfly.container.bean import bean
from pyfly.container import configuration
from pyfly.observability.metrics import MetricsRegistry

@configuration
class MyObservabilityConfig:
    @bean
    def metrics_registry(self) -> MetricsRegistry:
        return MetricsRegistry()  # Custom configuration
```

**Source:** `src/pyfly/observability/auto_configuration.py`

---

## Configuration

### Logging Settings

Configure logging in `pyfly.yaml`:

```yaml
pyfly:
  logging:
    level:
      root: INFO                    # Root log level
      sqlalchemy.engine: WARNING    # Silence SQLAlchemy query logs
      httpx: DEBUG                  # Verbose HTTP client logs
      myapp.services: DEBUG         # Debug your service layer
    format: console                 # "console" (dev) or "json" (prod)
```

The framework defaults (from `pyfly-defaults.yaml`) are:

```yaml
pyfly:
  logging:
    level:
      root: INFO
    format: console
```

Profile-specific overrides work as expected. For example, create a
`pyfly-production.yaml`:

```yaml
pyfly:
  logging:
    level:
      root: WARNING
    format: json
```

Environment variables can also override logging settings. The variable name follows
the pattern `PYFLY_LOGGING_LEVEL_ROOT=WARNING`.

### Metrics and Actuator Settings

Enable the actuator (which includes a health endpoint) via configuration:

```yaml
pyfly:
  web:
    actuator:
      enabled: true
```

The framework default is `enabled: false`. You can also enable it programmatically
when creating the web application:

```python
from pyfly.web.adapters.starlette import create_app

app = create_app(
    title="Order Service",
    version="1.0.0",
    context=context,
    actuator_enabled=True,
)
```

This registers the `/actuator/health`, `/actuator/beans`, `/actuator/env`, and
`/actuator/info` endpoints. See the [Actuator Guide](actuator.md) for full details.

---

## Complete Example

The following example demonstrates all four observability pillars working together
in a single service.

```python
"""order_service/app.py -- Full observability example."""

from pyfly.core import pyfly_application, PyFlyApplication
from pyfly.container import service, rest_controller
from pyfly.web import request_mapping, post_mapping, get_mapping, Body
from pyfly.web.adapters.starlette import create_app
from pyfly.observability import (
    MetricsRegistry, timed, counted, span,
    configure_logging, get_logger,
    HealthChecker,
)
from pydantic import BaseModel


# =========================================================================
# 1. Logging -- configure once at startup
# =========================================================================

configure_logging(level="INFO", json_output=False)
logger = get_logger("order_service")


# =========================================================================
# 2. Metrics -- create a registry and define metrics
# =========================================================================

registry = MetricsRegistry()

orders_counter = registry.counter(
    "orders_total",
    "Total orders processed",
    labels=["status"],
)

order_duration = registry.histogram(
    "order_processing_seconds",
    "Time to process an order",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)


# =========================================================================
# 3. Health Checks -- register component checks
# =========================================================================

checker = HealthChecker()

async def db_health() -> bool:
    """Check database connectivity."""
    try:
        # Replace with your actual database ping
        return True
    except Exception:
        return False

async def payment_gateway_health() -> bool:
    """Check payment gateway reachability."""
    try:
        # Replace with an actual HTTP ping
        return True
    except Exception:
        return False

checker.add_check("database", db_health)
checker.add_check("payment_gateway", payment_gateway_health)


# =========================================================================
# 4. Request/Response Models
# =========================================================================

class CreateOrderRequest(BaseModel):
    customer_id: str
    items: list[dict]


# =========================================================================
# 5. Service Layer -- with tracing, metrics, and logging
# =========================================================================

@service
class OrderService:

    @timed(registry, "create_order_seconds", "Time to create an order")
    @counted(registry, "create_order_total", "Orders created")
    @span("create-order")
    async def create_order(self, customer_id: str, items: list[dict]) -> dict:
        logger.info("creating_order",
            customer_id=customer_id,
            item_count=len(items),
        )

        # ... business logic here ...
        order_id = "ord-12345"

        logger.info("order_created",
            order_id=order_id,
            customer_id=customer_id,
        )

        orders_counter.labels(status="created").inc()
        return {"order_id": order_id, "status": "created"}

    @span("validate-payment")
    async def validate_payment(self, order_id: str, amount: float) -> bool:
        logger.info("validating_payment", order_id=order_id, amount=amount)
        return True


# =========================================================================
# 6. Controller -- the HTTP entry point
# =========================================================================

@rest_controller
@request_mapping("/api/orders")
class OrderController:
    def __init__(self, order_service: OrderService) -> None:
        self._service = order_service

    @post_mapping("", status_code=201)
    async def create(self, body: Body[CreateOrderRequest]) -> dict:
        return await self._service.create_order(
            customer_id=body.customer_id,
            items=body.items,
        )


# =========================================================================
# 7. Application Bootstrap
# =========================================================================

@pyfly_application(
    name="order-service",
    version="1.0.0",
    scan_packages=["order_service"],
)
class Application:
    pass


async def main():
    pyfly_app = PyFlyApplication(Application)
    await pyfly_app.startup()

    # Create the web app with actuator enabled for /actuator/health
    app = create_app(
        title="Order Service",
        version="1.0.0",
        context=pyfly_app.context,
        actuator_enabled=True,
    )

    # Programmatic health check
    result = await checker.check()
    logger.info("health_check_result",
        status=result.status.value,
        checks={name: s.value for name, s in result.checks.items()},
    )

    await pyfly_app.shutdown()
```

**Console output** when running this example:

```
2026-01-15T10:30:00.000Z [info    ] starting_application   app=order-service version=1.0.0
2026-01-15T10:30:00.002Z [info    ] active_profiles        profiles=[]
2026-01-15T10:30:00.010Z [info    ] application_started    app=order-service startup_time_s=0.01 beans_initialized=2
2026-01-15T10:30:00.015Z [info    ] health_check_result    status=UP checks={'database': 'UP', 'payment_gateway': 'UP'}
```

**JSON output** (in production with `json_output=True`):

```json
{"event": "creating_order", "customer_id": "cust-42", "item_count": 2, "timestamp": "2026-01-15T10:30:00Z", "level": "info", "logger": "order_service"}
{"event": "order_created", "order_id": "ord-12345", "customer_id": "cust-42", "timestamp": "2026-01-15T10:30:00Z", "level": "info", "logger": "order_service"}
```

Each log line is a self-contained JSON object ready for ingestion by log aggregation
systems such as Elasticsearch, Datadog, or Grafana Loki.
