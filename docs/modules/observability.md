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
   - [Quick Start with get_logger](#quick-start-with-get_logger)
   - [LoggingPort Protocol](#loggingport-protocol)
   - [StructlogAdapter](#structlogadapter)
   - [Structured Logging with Key-Value Pairs](#structured-logging-with-key-value-pairs)
   - [Correlation IDs](#correlation-ids)
5. [Health Checks](#health-checks)
   - [HealthAggregator](#healthaggregator)
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

The core observability utilities come from two packages:

```python
# Metrics and tracing
from pyfly.observability import (
    MetricsRegistry, timed, counted,   # Metrics (requires pyfly[observability])
    span,                               # Tracing (requires opentelemetry)
)

# Structured logging
from pyfly.logging import get_logger

# Health checks (production-grade, with HealthAggregator)
from pyfly.actuator import (
    HealthIndicator, HealthStatus, HealthResult, HealthAggregator,
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

Internally the registry maintains three dictionaries:

```python
self._counters: dict[str, Counter] = {}
self._histograms: dict[str, Histogram] = {}
self._gauges: dict[str, Gauge] = {}
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

The `@timed` decorator records the execution duration of an **async or sync** function
as a histogram observation. It uses Micrometer-style dot.case names and automatically
tags each observation with `class`, `method`, and `exception` labels.

```python
from pyfly.observability import MetricsRegistry, timed

registry = MetricsRegistry()

@timed(registry, "orders.process", "Time to process an order")
async def process_order(order_id: str) -> dict:
    # ... business logic ...
    return {"order_id": order_id, "status": "processed"}
```

**How it works internally:**

1. Records `start = time.perf_counter()`.
2. Calls the decorated function inside a `try/except/finally` block.
3. In the `finally` clause, observes the elapsed time on a labeled histogram —
   the `exception` label is `"none"` on success or the exception type name on failure.

The duration is recorded regardless of success or failure. The histogram name uses
Micrometer dot.case convention and gets a `_seconds` suffix if not already present
(e.g. `"orders.process"` → Prometheus name `orders_process_seconds`).

**Parameters:**

| Parameter     | Type              | Default | Description                               |
|---------------|-------------------|---------|-------------------------------------------|
| `registry`    | `MetricsRegistry` | required | The registry that owns the histogram     |
| `name`        | `str`             | `"method.timed"` | Micrometer dot.case meter name  |
| `description` | `str`             | `"Timed method execution"` | Human-readable description |
| `extra_tags`  | `dict[str, str] \| None` | `None` | Additional Prometheus labels      |

### @counted Decorator

The `@counted` decorator increments a counter each time an **async or sync** function
completes (success or failure).

```python
from pyfly.observability import MetricsRegistry, counted

registry = MetricsRegistry()

@counted(registry, "orders.created", "Total orders created")
async def create_order(data: dict) -> dict:
    # ... business logic ...
    return {"id": "ord-123", **data}
```

**How it works internally:**

1. Calls the decorated function.
2. On success, increments the counter with labels `result="success"`, `exception="none"`.
3. On failure, increments with `result="failure"`, `exception=<ExceptionTypeName>` and
   re-raises.

The counter is labeled with `class`, `method`, `result`, and `exception` and uses
Micrometer dot.case naming (prometheus_client appends `_total` automatically).

**Parameters:**

| Parameter     | Type              | Default | Description                               |
|---------------|-------------------|---------|-------------------------------------------|
| `registry`    | `MetricsRegistry` | required | The registry that owns the counter       |
| `name`        | `str`             | `"method.counted"` | Micrometer dot.case meter name |
| `description` | `str`             | `"Counted method invocations"` | Human-readable description |
| `extra_tags`  | `dict[str, str] \| None` | `None` | Additional Prometheus labels      |

### Combining @timed and @counted

You can stack both decorators on the same function:

```python
@timed(registry, "orders.duration", "Order processing time")
@counted(registry, "orders.processed", "Orders processed")
async def process_order(order_id: str) -> dict:
    ...
```

Both decorators support async and sync functions. Stacking them means each
invocation produces both a timer observation and a counter increment.

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

The `@span` decorator wraps an **async or sync** function in an OpenTelemetry span. This
enables distributed tracing across service boundaries -- each span records the
function's name, timing, and any errors that occur.

```python
from pyfly.observability import span

@span("fetch-inventory")
async def fetch_inventory(sku: str) -> dict:
    # ... call inventory service ...
    return {"sku": sku, "quantity": 42}

# Sync functions are also supported
@span("validate-input")
def validate_input(data: dict) -> bool:
    return bool(data.get("id"))
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

The wrapper implementation (async path shown; sync path is identical without `await`):

```python
@functools.wraps(func)
async def wrapper(*args: Any, **kwargs: Any) -> Any:
    with _tracer.start_as_current_span(name) as current_span:
        try:
            return await func(*args, **kwargs)
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

PyFly provides structured logging through the `pyfly.logging` hexagonal port
with `LoggingPort` (protocol) and `StructlogAdapter` (default implementation).
Both are backed by [structlog](https://www.structlog.org/) for structured, key-value
logging.

### Quick Start with get_logger

Use `get_logger()` from `pyfly.logging` to obtain a named structured logger anywhere
in your code. The logging system is configured automatically during application
bootstrap; no manual setup call is needed:

```python
from pyfly.logging import get_logger

logger = get_logger("order_service")

logger.info("order_created", order_id="ord-123", customer="acme")
logger.warning("inventory_low", sku="WIDGET-42", remaining=3)
logger.error("payment_failed", order_id="ord-123", reason="declined")
```

**`get_logger()` Parameters:**

| Parameter | Type  | Description              |
|-----------|-------|--------------------------|
| `name`    | `str` | The logger name          |

Returns a `structlog.stdlib.BoundLogger` when structlog is installed, or a lightweight
stdlib-backed shim otherwise. Both accept the same `(event, **kwargs)` call signature.

**Source:** `src/pyfly/logging/__init__.py`

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
| `pyfly.logging.format`        | Output format: `"console"`, `"json"`, or `"logfmt"` | `"console"` |

When `configure()` is called, the adapter performs these steps:

1. Reads the `pyfly.logging.level` section from config.
2. Extracts the `root` level and collects per-module overrides.
3. Reads `pyfly.logging.format` to determine the output renderer (`console`, `json`, or `logfmt`).
4. Configures structlog processors with a `ProcessorFormatter` and `foreign_pre_chain` for unified formatting of all loggers (framework, application, and third-party).
5. Sets up the root logger's level and handlers.
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

PyFly's production health check system lives in `pyfly.actuator`. See the
[Actuator Guide](actuator.md) for the full reference (`HealthAggregator`,
`HealthIndicator`, `HealthStatus`, `HealthResult`). Below is a quick overview.

### HealthAggregator

`HealthAggregator` collects `HealthIndicator` beans and runs them to produce an
aggregated result. It is typically set up automatically by actuator auto-configuration.

```python
from pyfly.actuator import HealthAggregator, HealthStatus, HealthIndicator
from pyfly.container import component

@component
class DatabaseHealthIndicator:
    async def health(self) -> HealthStatus:
        try:
            await database.execute("SELECT 1")
            return HealthStatus(status="UP", details={"type": "postgresql"})
        except Exception as e:
            return HealthStatus(status="DOWN", details={"error": str(e)})

# Use aggregator directly
aggregator = HealthAggregator()
aggregator.add_indicator("database", DatabaseHealthIndicator())

result = await aggregator.check()
print(result.status)      # "UP" or "DOWN"
print(result.components)  # {"database": HealthStatus(status="UP", ...)}
```

`HealthStatus` values are: `"UP"`, `"DOWN"`, `"OUT_OF_SERVICE"`, `"UNKNOWN"`.

**Source:** `src/pyfly/actuator/health.py`

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

The following example demonstrates the three observability pillars — metrics, tracing,
and logging — working together in a single service, plus a custom health indicator.

```python
"""order_service/app.py -- Full observability example."""

from pyfly.core import pyfly_application, PyFlyApplication
from pyfly.container import service, component, rest_controller
from pyfly.web import request_mapping, post_mapping, Body
from pyfly.web.adapters.starlette import create_app
from pyfly.observability import MetricsRegistry, timed, counted, span
from pyfly.logging import get_logger
from pyfly.actuator import HealthStatus
from pydantic import BaseModel


# =========================================================================
# 1. Logging -- get a logger (configured automatically by the framework)
# =========================================================================

logger = get_logger("order_service")


# =========================================================================
# 2. Metrics -- create a registry and define metrics
# =========================================================================

registry = MetricsRegistry()

orders_counter = registry.counter(
    "orders.created",
    "Total orders processed",
    labels=["status"],
)


# =========================================================================
# 3. Health Indicator -- contributes to /actuator/health
# =========================================================================

@component
class DatabaseHealthIndicator:
    async def health(self) -> HealthStatus:
        try:
            # Replace with your actual database ping
            return HealthStatus(status="UP", details={"type": "postgresql"})
        except Exception as e:
            return HealthStatus(status="DOWN", details={"error": str(e)})


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

    @timed(registry, "orders.process", "Time to process an order")
    @counted(registry, "orders.processed", "Orders processed")
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

    await pyfly_app.shutdown()
```

**JSON output** (in production with `pyfly.logging.format: json`):

```json
{"event": "creating_order", "customer_id": "cust-42", "item_count": 2, "timestamp": "2026-01-15T10:30:00Z", "level": "info", "logger": "order_service"}
{"event": "order_created", "order_id": "ord-12345", "customer_id": "cust-42", "timestamp": "2026-01-15T10:30:00Z", "level": "info", "logger": "order_service"}
```

Each log line is a self-contained JSON object ready for ingestion by log aggregation
systems such as Elasticsearch, Datadog, or Grafana Loki.
