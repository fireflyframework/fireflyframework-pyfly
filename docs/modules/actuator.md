# Actuator Guide

The actuator module provides production-ready monitoring and management endpoints
for PyFly applications. Inspired by Spring Boot Actuator, it exposes HTTP endpoints
that reveal the health, configuration, and composition of your running application.

The actuator is built on an extensible endpoint architecture. Built-in endpoints
cover health checks, bean inspection, environment profiles, application info,
logger management, and metrics. You can also create custom endpoints that are
automatically discovered from the DI container.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Enabling Actuator](#enabling-actuator)
   - [Via create_app()](#via-create_app)
   - [Via pyfly.yaml](#via-pyflyyaml)
3. [Built-in Endpoints Summary](#built-in-endpoints-summary)
4. [ActuatorEndpoint Protocol](#actuatorendpoint-protocol)
5. [ActuatorRegistry](#actuatorregistry)
   - [register()](#register)
   - [get_enabled_endpoints()](#get_enabled_endpoints)
   - [discover_from_context()](#discover_from_context)
   - [Per-endpoint Configuration](#per-endpoint-configuration)
6. [Index Endpoint](#index-endpoint)
7. [Health Endpoint](#health-endpoint)
   - [Response Format](#response-format)
   - [HTTP Status Codes](#http-status-codes)
8. [HealthIndicator Protocol](#healthindicator-protocol)
9. [HealthStatus Dataclass](#healthstatus-dataclass)
10. [HealthAggregator](#healthaggregator)
    - [add_indicator()](#add_indicator)
    - [check()](#check)
    - [Aggregation Rules](#aggregation-rules)
11. [HealthResult Dataclass](#healthresult-dataclass)
12. [Custom Health Indicators](#custom-health-indicators)
    - [Database Health Indicator](#database-health-indicator)
    - [Redis Health Indicator](#redis-health-indicator)
    - [External Service Health Indicator](#external-service-health-indicator)
13. [Beans Endpoint](#beans-endpoint)
14. [Environment Endpoint](#environment-endpoint)
15. [Info Endpoint](#info-endpoint)
16. [Loggers Endpoint](#loggers-endpoint)
    - [GET /actuator/loggers](#get-actuatorloggers)
    - [POST /actuator/loggers/{name}](#post-actuatorloggersname)
17. [Metrics Endpoint](#metrics-endpoint)
18. [Custom Actuator Endpoints](#custom-actuator-endpoints)
19. [make_starlette_actuator_routes()](#make_starlette_actuator_routes)
20. [Auto-Configuration](#auto-configuration)
21. [Configuration](#configuration)
22. [Complete Example](#complete-example)

---

## Introduction

In production, operators need answers to questions like:

- "Is this service healthy enough to receive traffic?"
- "What beans are registered in the application context?"
- "What configuration profile is active?"
- "What version of the service is running?"
- "What loggers are active and at what level?"
- "Can I change a logger's level without redeploying?"

The actuator module answers all of these through standard HTTP endpoints:

| Endpoint                | Methods   | Description                     |
|-------------------------|-----------|---------------------------------|
| `GET /actuator`          | GET       | HAL-style index of all enabled endpoints |
| `GET /actuator/health`   | GET       | Aggregated application health status |
| `GET /actuator/beans`    | GET       | Registered bean information     |
| `GET /actuator/env`      | GET       | Active profiles                 |
| `GET /actuator/info`     | GET       | Application metadata            |
| `/actuator/loggers`      | GET, POST | Logger configuration and runtime level changes |
| `GET /actuator/metrics`  | GET       | Metrics stub (disabled by default) |

```python
from pyfly.actuator import (
    ActuatorEndpoint,
    ActuatorRegistry,
    HealthIndicator,
    HealthStatus,
    HealthResult,
    HealthAggregator,
)
```

**Source:** `src/pyfly/actuator/__init__.py`

---

## Enabling Actuator

### Via create_app()

Pass `actuator_enabled=True` when creating the web application:

```python
from pyfly.web.adapters.starlette import create_app

app = create_app(
    title="Order Service",
    version="1.0.0",
    context=application_context,
    actuator_enabled=True,
)
```

When actuator is enabled with a `context`, the framework automatically:

1. Creates a `HealthAggregator` instance.
2. Scans the DI container for any beans that implement the `HealthIndicator`
   protocol and registers them with the aggregator.
3. Creates an `ActuatorRegistry` with the application config.
4. Registers all built-in endpoints (`health`, `beans`, `env`, `info`, `loggers`,
   `metrics`) with the registry.
5. Calls `registry.discover_from_context(context)` to auto-discover any custom
   `ActuatorEndpoint` beans from the DI container.
6. Passes the registry to `make_starlette_actuator_routes(registry)` which builds
   the HTTP routes for all enabled endpoints, including the `/actuator` index.

### Via pyfly.yaml

Set the actuator enabled flag in your configuration file:

```yaml
pyfly:
  web:
    actuator:
      enabled: true
```

The framework default (from `pyfly-defaults.yaml`) is `enabled: false`.

---

## Built-in Endpoints Summary

| Endpoint  | Path                | Methods   | Default State | Description                              |
|-----------|---------------------|-----------|---------------|------------------------------------------|
| health    | `/actuator/health`  | GET       | enabled       | Aggregated health status from all indicators |
| beans     | `/actuator/beans`   | GET       | enabled       | Registered bean information from DI container |
| env       | `/actuator/env`     | GET       | enabled       | Active configuration profiles            |
| info      | `/actuator/info`    | GET       | enabled       | Application name, version, description   |
| loggers   | `/actuator/loggers` | GET, POST | enabled       | Logger configuration and runtime level changes |
| metrics   | `/actuator/metrics` | GET       | **disabled**  | Metrics stub for future Prometheus/OpenTelemetry integration |

Any endpoint can be enabled or disabled individually via configuration. See
[Per-endpoint Configuration](#per-endpoint-configuration).

---

## ActuatorEndpoint Protocol

`ActuatorEndpoint` is the extensible foundation of the actuator system. Every
actuator endpoint -- built-in or custom -- implements this runtime-checkable
protocol.

```python
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class ActuatorEndpoint(Protocol):
    @property
    def endpoint_id(self) -> str:
        """URL path suffix: /actuator/{endpoint_id}."""
        ...

    @property
    def enabled(self) -> bool:
        """Default enable state. Can be overridden via config."""
        ...

    async def handle(self, context: Any = None) -> dict[str, Any]:
        """Handle a request and return a JSON-serializable dict."""
        ...
```

| Property / Method | Type                  | Description                                    |
|-------------------|-----------------------|------------------------------------------------|
| `endpoint_id`     | `str` (property)      | Determines the URL: `/actuator/{endpoint_id}`  |
| `enabled`         | `bool` (property)     | Default enable state; can be overridden by config |
| `handle()`        | `async` method        | Processes a request and returns a JSON-serializable dict |

Any class that has these three members satisfies the protocol via structural
subtyping. No explicit inheritance is required.

**Source:** `src/pyfly/actuator/ports.py`

---

## ActuatorRegistry

`ActuatorRegistry` collects and manages `ActuatorEndpoint` instances. It is the
central registry that the Starlette adapter reads from when building HTTP routes.

```python
from pyfly.actuator import ActuatorRegistry

registry = ActuatorRegistry(config=application_context.config)
```

**Constructor Parameters:**

| Parameter | Type              | Default | Description                              |
|-----------|-------------------|---------|------------------------------------------|
| `config`  | `Config \| None`  | `None`  | Application config for per-endpoint overrides |

### register()

Register an `ActuatorEndpoint` with the registry:

```python
registry.register(HealthEndpoint(aggregator))
registry.register(BeansEndpoint(context))
registry.register(LoggersEndpoint())
```

If an endpoint with the same `endpoint_id` is already registered, it is replaced.

### get_enabled_endpoints()

Returns a dictionary of all endpoints that are currently enabled:

```python
enabled = registry.get_enabled_endpoints()
# {"health": <HealthEndpoint>, "beans": <BeansEndpoint>, ...}
```

Enable state is determined by (highest priority first):

1. Config key `pyfly.actuator.endpoints.{endpoint_id}.enabled`
2. The endpoint's own `enabled` property

### discover_from_context()

Auto-discover `ActuatorEndpoint` beans from the DI container:

```python
registry.discover_from_context(application_context)
```

This method iterates over all bean registrations in the application context. Any
bean instance that satisfies the `ActuatorEndpoint` protocol and whose
`endpoint_id` is not already in the registry is automatically registered. This is
how custom endpoints decorated with `@component` are picked up.

### Per-endpoint Configuration

Each endpoint can be individually enabled or disabled via `pyfly.yaml`, regardless
of the endpoint's default `enabled` property:

```yaml
pyfly:
  actuator:
    endpoints:
      metrics:
        enabled: true     # Enable the metrics stub (disabled by default)
      beans:
        enabled: false    # Disable the beans endpoint
      loggers:
        enabled: false    # Disable the loggers endpoint
```

The config key pattern is `pyfly.actuator.endpoints.{endpoint_id}.enabled`. The
accepted values are `true`, `false`, `1`, `0`, `yes`, and `no`.

**Source:** `src/pyfly/actuator/registry.py`

---

## Index Endpoint

**Endpoint:** `GET /actuator`

The index endpoint provides a HAL-style directory of all enabled actuator
endpoints. It is always present when the actuator is enabled and is generated
automatically by the Starlette adapter.

**Response format:**

```json
{
    "_links": {
        "self": {"href": "/actuator"},
        "health": {"href": "/actuator/health"},
        "beans": {"href": "/actuator/beans"},
        "env": {"href": "/actuator/env"},
        "info": {"href": "/actuator/info"},
        "loggers": {"href": "/actuator/loggers"}
    }
}
```

Only enabled endpoints appear in `_links`. If you disable an endpoint via
configuration, it will not appear in the index. If you enable the `metrics`
endpoint, it will appear alongside the others.

**Source:** `src/pyfly/actuator/adapters/starlette.py`

---

## Health Endpoint

**Endpoint:** `GET /actuator/health`

The health endpoint runs all registered health indicators and returns an aggregated
health result. It is implemented by the `HealthEndpoint` class.

### Response Format

When all components are healthy:

```json
{
    "status": "UP",
    "components": {
        "database": {
            "status": "UP",
            "details": {
                "type": "postgresql",
                "version": "16.1"
            }
        },
        "redis": {
            "status": "UP",
            "details": {
                "cluster_mode": false
            }
        }
    }
}
```

When one or more components are unhealthy:

```json
{
    "status": "DOWN",
    "components": {
        "database": {
            "status": "UP",
            "details": {"type": "postgresql"}
        },
        "redis": {
            "status": "DOWN",
            "details": {"error": "Connection refused"}
        }
    }
}
```

When no health indicators are registered:

```json
{
    "status": "UP"
}
```

### HTTP Status Codes

The health endpoint returns dynamic HTTP status codes based on the aggregated
health state:

| Overall Status | HTTP Code                 | Meaning                                  |
|----------------|---------------------------|------------------------------------------|
| `"UP"`         | `200 OK`                  | All components are healthy               |
| `"DOWN"`       | `503 Service Unavailable` | One or more components are unhealthy     |

The `HealthEndpoint` class provides a `get_status_code()` method used by the
Starlette adapter:

```python
class HealthEndpoint:
    async def handle(self, context=None) -> dict[str, Any]:
        result = await self._aggregator.check()
        return result.to_dict()

    async def get_status_code(self) -> int:
        result = await self._aggregator.check()
        return 503 if result.status == "DOWN" else 200
```

**Source:** `src/pyfly/actuator/endpoints/health_endpoint.py`

---

## HealthIndicator Protocol

`HealthIndicator` is a runtime-checkable Protocol that any bean can implement to
contribute health information to the actuator.

```python
from pyfly.actuator import HealthIndicator, HealthStatus

@runtime_checkable
class HealthIndicator(Protocol):
    async def health(self) -> HealthStatus: ...
```

Any class with an `async def health(self) -> HealthStatus` method satisfies this
protocol. When actuator is enabled, the framework scans for beans implementing this
protocol and automatically registers them as health indicators.

**Source:** `src/pyfly/actuator/health.py`

---

## HealthStatus Dataclass

`HealthStatus` represents the health of a single component.

```python
from pyfly.actuator import HealthStatus

@dataclass
class HealthStatus:
    status: str                            # "UP", "DOWN", or "DEGRADED"
    details: dict[str, Any] = field(default_factory=dict)
```

| Field     | Type              | Description                                   |
|-----------|-------------------|-----------------------------------------------|
| `status`  | `str`             | Health state: `"UP"`, `"DOWN"`, or `"DEGRADED"` |
| `details` | `dict[str, Any]`  | Additional information (version, type, error, etc.) |

**Usage:**

```python
# Healthy component
HealthStatus(status="UP", details={"type": "postgresql", "version": "16.1"})

# Unhealthy component
HealthStatus(status="DOWN", details={"error": "Connection timed out"})

# Degraded component
HealthStatus(status="DEGRADED", details={"active_connections": 95, "max": 100})
```

---

## HealthAggregator

`HealthAggregator` collects health indicators from multiple components and produces
an aggregated `HealthResult`.

```python
from pyfly.actuator import HealthAggregator

aggregator = HealthAggregator()
```

### add_indicator()

Register a named health indicator:

```python
aggregator.add_indicator("database", DatabaseHealthIndicator())
aggregator.add_indicator("redis", RedisHealthIndicator())
aggregator.add_indicator("payment-gateway", PaymentGatewayHealthIndicator())
```

**Parameters:**

| Parameter   | Type              | Description                        |
|-------------|-------------------|------------------------------------|
| `name`      | `str`             | Unique name for this indicator     |
| `indicator` | `HealthIndicator` | Object implementing the protocol   |

### check()

Run all registered health indicators and return an aggregated result:

```python
result = await aggregator.check()
print(result.status)      # "UP" or "DOWN"
print(result.components)  # {"database": HealthStatus(...), "redis": HealthStatus(...)}
```

**Returns:** A `HealthResult` dataclass.

### Aggregation Rules

The aggregator follows these rules:

1. **All UP:** If every indicator reports `"UP"`, the overall status is `"UP"`.
2. **Any DOWN:** If any indicator reports `"DOWN"`, the overall status is `"DOWN"`.
3. **Exception:** If an indicator's `health()` method raises an exception, that
   indicator is treated as `"DOWN"` with `details={"error": "check failed"}`.
   The exception is logged but does not crash the health check.
4. **No indicators:** If no indicators are registered, the overall status is `"UP"`.

```python
if not self._indicators:
    return HealthResult(status="UP")

for name, indicator in self._indicators.items():
    try:
        status = await indicator.health()
        components[name] = status
        if status.status == "DOWN":
            overall = "DOWN"
    except Exception:
        logger.exception("Health indicator '%s' raised an exception", name)
        components[name] = HealthStatus(status="DOWN", details={"error": "check failed"})
        overall = "DOWN"
```

**Source:** `src/pyfly/actuator/health.py`

---

## HealthResult Dataclass

`HealthResult` is the aggregated result returned by `HealthAggregator.check()`.

```python
from pyfly.actuator import HealthResult

@dataclass
class HealthResult:
    status: str
    components: dict[str, HealthStatus] = field(default_factory=dict)
```

| Field        | Type                          | Description                           |
|-------------|-------------------------------|---------------------------------------|
| `status`    | `str`                         | Overall status: `"UP"` or `"DOWN"`    |
| `components` | `dict[str, HealthStatus]`    | Per-component health status           |

### to_dict()

Serializes the result to a JSON-friendly dictionary:

```python
result = HealthResult(
    status="UP",
    components={
        "database": HealthStatus(status="UP", details={"type": "postgresql"}),
        "redis": HealthStatus(status="UP"),
    },
)

result.to_dict()
# {
#     "status": "UP",
#     "components": {
#         "database": {"status": "UP", "details": {"type": "postgresql"}},
#         "redis": {"status": "UP", "details": {}}
#     }
# }
```

If there are no components, the `"components"` key is omitted:

```python
HealthResult(status="UP").to_dict()
# {"status": "UP"}
```

---

## Custom Health Indicators

### Database Health Indicator

```python
from pyfly.actuator import HealthIndicator, HealthStatus
from pyfly.container import component


@component
class DatabaseHealthIndicator:
    """Checks database connectivity by executing a lightweight query."""

    def __init__(self, db_session_factory) -> None:
        self._session_factory = db_session_factory

    async def health(self) -> HealthStatus:
        try:
            async with self._session_factory() as session:
                await session.execute("SELECT 1")
            return HealthStatus(
                status="UP",
                details={
                    "type": "postgresql",
                    "version": "16.1",
                },
            )
        except Exception as e:
            return HealthStatus(
                status="DOWN",
                details={"error": str(e)},
            )
```

### Redis Health Indicator

```python
@component
class RedisHealthIndicator:
    """Checks Redis connectivity with a PING command."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def health(self) -> HealthStatus:
        try:
            await self._redis.ping()
            info = await self._redis.info("server")
            return HealthStatus(
                status="UP",
                details={
                    "version": info.get("redis_version", "unknown"),
                    "cluster_mode": info.get("cluster_enabled", "0") == "1",
                },
            )
        except Exception as e:
            return HealthStatus(
                status="DOWN",
                details={"error": str(e)},
            )
```

### External Service Health Indicator

```python
import httpx


@component
class PaymentGatewayHealthIndicator:
    """Checks connectivity to an external payment gateway."""

    async def health(self) -> HealthStatus:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get("https://api.payment-gateway.com/health")
                if response.status_code == 200:
                    return HealthStatus(
                        status="UP",
                        details={"response_time_ms": response.elapsed.total_seconds() * 1000},
                    )
                return HealthStatus(
                    status="DOWN",
                    details={"http_status": response.status_code},
                )
        except httpx.RequestError as e:
            return HealthStatus(
                status="DOWN",
                details={"error": str(e)},
            )
```

Because these classes are decorated with `@component`, they are automatically
discovered during package scanning. Because they implement the `HealthIndicator`
protocol (they have `async def health() -> HealthStatus`), the actuator
automatically registers them as indicators.

---

## Beans Endpoint

**Endpoint:** `GET /actuator/beans`

Returns information about all beans registered in the DI container.

**Response format:**

```json
{
    "beans": {
        "OrderService": {
            "type": "order_service.services.OrderService",
            "scope": "SINGLETON",
            "stereotype": "service"
        },
        "OrderRepository": {
            "type": "order_service.repositories.OrderRepository",
            "scope": "SINGLETON",
            "stereotype": "repository"
        },
        "OrderController": {
            "type": "order_service.controllers.OrderController",
            "scope": "SINGLETON",
            "stereotype": "rest_controller"
        }
    }
}
```

Each bean entry contains:

| Field        | Description                                           |
|-------------|-------------------------------------------------------|
| `type`      | Fully qualified class name (`module.ClassName`)        |
| `scope`     | Lifecycle scope: `SINGLETON`, `TRANSIENT`, or `REQUEST` |
| `stereotype` | Stereotype: `component`, `service`, `repository`, `controller`, `rest_controller`, `configuration`, or `none` |

**Source:** `src/pyfly/actuator/endpoints/beans_endpoint.py`

---

## Environment Endpoint

**Endpoint:** `GET /actuator/env`

Returns the active configuration profiles.

**Response format:**

```json
{
    "activeProfiles": ["dev", "local"]
}
```

If no profiles are active:

```json
{
    "activeProfiles": []
}
```

Profiles are set via `pyfly.yaml` or the `PYFLY_PROFILES_ACTIVE` environment
variable:

```yaml
pyfly:
  profiles:
    active: dev,local
```

```bash
export PYFLY_PROFILES_ACTIVE=production
```

**Source:** `src/pyfly/actuator/endpoints/env_endpoint.py`

---

## Info Endpoint

**Endpoint:** `GET /actuator/info`

Returns application metadata from the configuration.

**Response format:**

```json
{
    "app": {
        "name": "order-service",
        "version": "1.0.0",
        "description": "Order management microservice"
    }
}
```

The values come from the `pyfly.app` configuration section:

```yaml
pyfly:
  app:
    name: order-service
    version: 1.0.0
    description: Order management microservice
```

**Source:** `src/pyfly/actuator/endpoints/info_endpoint.py`

---

## Loggers Endpoint

The loggers endpoint exposes logger configuration and supports changing log levels
at runtime without a restart. It is implemented by the `LoggersEndpoint` class.

**Source:** `src/pyfly/actuator/endpoints/loggers_endpoint.py`

### GET /actuator/loggers

Lists all registered loggers with their configured and effective levels.

**Response format:**

```json
{
    "loggers": {
        "ROOT": {
            "configuredLevel": "INFO",
            "effectiveLevel": "INFO"
        },
        "pyfly.web": {
            "configuredLevel": null,
            "effectiveLevel": "INFO"
        },
        "pyfly.actuator": {
            "configuredLevel": "DEBUG",
            "effectiveLevel": "DEBUG"
        },
        "order_service.controllers": {
            "configuredLevel": null,
            "effectiveLevel": "INFO"
        }
    },
    "levels": ["OFF", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"],
    "groups": {
        "web": {"configuredLevel": null, "members": ["pyfly.web", "uvicorn", "starlette"]},
        "sql": {"configuredLevel": null, "members": ["sqlalchemy", "pyfly.data"]}
    }
}
```

The endpoint uses Spring Boot's level vocabulary (`OFF`, `ERROR`, `WARN`,
`INFO`, `DEBUG`, `TRACE`) rather than Python's, so the response is drop-in
compatible with Spring Boot tooling. Python's `CRITICAL` collapses to `ERROR`;
`TRACE` maps to numeric level 5 and `OFF` to a disabled logger.

| Field                | Description                                           |
|---------------------|-------------------------------------------------------|
| `loggers`           | Dictionary of logger name to level information        |
| `configuredLevel`   | Explicitly set level, or `null` if inheriting from parent |
| `effectiveLevel`    | Actual level in effect (may be inherited)             |
| `levels`            | List of all recognized level names                    |
| `groups`            | Built-in logger groups (`web`, `sql`) and their members |

### POST /actuator/loggers/{name}

Changes a logger's level at runtime. The logger name is a **path parameter**;
send a JSON body with the desired `configuredLevel`:

```
POST /actuator/loggers/pyfly.web
```

**Request body:**

```json
{
    "configuredLevel": "DEBUG"
}
```

**Success response:** `204 No Content` (empty body).

To **reset** a logger to inherit from its parent (NOTSET), send `null` (or omit
the field):

```json
{
    "configuredLevel": null
}
```

**Error response (400) -- unknown level:**

```json
{
    "error": "Unknown level: INVALID. Valid levels: OFF, ERROR, WARN, INFO, DEBUG, TRACE"
}
```

Use `ROOT` as the logger name to change the root logger level:

```
POST /actuator/loggers/ROOT
```

A single logger's levels can also be inspected with
`GET /actuator/loggers/{name}`, which returns `{configuredLevel, effectiveLevel}`.

**Example with curl:**

```bash
# List all loggers
curl http://localhost:8080/actuator/loggers

# Change pyfly.web logger to DEBUG
curl -X POST http://localhost:8080/actuator/loggers/pyfly.web \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": "DEBUG"}'

# Change root logger to WARN
curl -X POST http://localhost:8080/actuator/loggers/ROOT \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": "WARN"}'

# Reset pyfly.web to inherit from its parent
curl -X POST http://localhost:8080/actuator/loggers/pyfly.web \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": null}'
```

---

## Metrics Endpoint

**Endpoint:** `GET /actuator/metrics`

The metrics endpoint is a stub for future Prometheus and OpenTelemetry integration.
It is **disabled by default** and must be explicitly enabled via configuration:

```yaml
pyfly:
  actuator:
    endpoints:
      metrics:
        enabled: true
```

**Response format (when enabled):**

```json
{
    "names": [],
    "message": "Metrics endpoint stub. Configure a metrics backend to populate."
}
```

Once a metrics backend is integrated, this endpoint will expose application metrics
such as request counts, response times, JVM-like runtime statistics, and custom
counters/gauges.

**Source:** `src/pyfly/actuator/endpoints/metrics_endpoint.py`

---

## Custom Actuator Endpoints

You can create custom actuator endpoints by implementing the `ActuatorEndpoint`
protocol and registering the class as a `@component` bean. The actuator will
automatically discover and register your endpoint during application startup.

### Creating a Custom Endpoint

```python
from pyfly.container import component
from pyfly.actuator import ActuatorEndpoint


@component
class GitInfoEndpoint:
    """Exposes git commit information at /actuator/git."""

    @property
    def endpoint_id(self) -> str:
        return "git"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context=None) -> dict:
        return {
            "branch": "main",
            "commit": {
                "id": "abc123def",
                "time": "2026-01-15T10:30:00Z",
            },
        }
```

This endpoint will be:

1. Discovered by component scanning because of the `@component` decorator.
2. Picked up by `registry.discover_from_context(context)` because it satisfies
   the `ActuatorEndpoint` protocol.
3. Mounted at `GET /actuator/git`.
4. Listed in the `GET /actuator` index response.

### Another Example: System Info Endpoint

```python
import platform

from pyfly.container import component


@component
class SystemInfoEndpoint:
    """Exposes system-level information at /actuator/system."""

    @property
    def endpoint_id(self) -> str:
        return "system"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context=None) -> dict:
        return {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "architecture": platform.machine(),
        }
```

### Disabling a Custom Endpoint via Config

Custom endpoints respect the same per-endpoint configuration as built-in endpoints:

```yaml
pyfly:
  actuator:
    endpoints:
      git:
        enabled: false   # Disable the custom git endpoint
```

---

## make_starlette_actuator_routes()

`make_starlette_actuator_routes()` builds the HTTP route objects from an
`ActuatorRegistry`. It is called internally by `create_app()` when
`actuator_enabled=True`, but you can also call it directly for advanced use cases.

```python
from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
from pyfly.actuator import ActuatorRegistry, HealthAggregator
from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint

aggregator = HealthAggregator()
aggregator.add_indicator("database", DatabaseHealthIndicator())

registry = ActuatorRegistry(config=application_context.config)
registry.register(HealthEndpoint(aggregator))

routes = make_starlette_actuator_routes(registry)
```

**Parameters:**

| Parameter  | Type               | Description                                 |
|------------|--------------------|---------------------------------------------|
| `registry` | `ActuatorRegistry` | Registry containing all actuator endpoints  |

**Behavior:**

- Creates a `GET /actuator` index endpoint that lists all enabled endpoints as
  HAL-style `_links`.
- For each enabled endpoint in the registry, creates the appropriate routes:
  - `HealthEndpoint` gets special handling for dynamic status codes (200/503).
  - `LoggersEndpoint` gets two routes: `GET` for listing and `POST` for level
    changes.
  - All other endpoints get a generic `GET` handler that calls `handle()` and
    returns a 200 JSON response.

**Source:** `src/pyfly/actuator/adapters/starlette.py`

---

## Auto-Configuration

When actuator is enabled in configuration, PyFly automatically registers all actuator beans through two auto-configuration classes. This replaces the need for manual setup in `create_app()`.

### ActuatorAutoConfiguration

**Conditions:** `pyfly.web.actuator.enabled=true`.

| Bean | Type | Description |
|------|------|-------------|
| `actuator_registry` | `ActuatorRegistry` | Central registry for all actuator endpoints |
| `health_aggregator` | `HealthAggregator` | Collects and aggregates health indicator results |

### MetricsActuatorAutoConfiguration

**Conditions:** `pyfly.web.actuator.enabled=true` AND `prometheus_client` library installed.

| Bean | Type | Description |
|------|------|-------------|
| `metrics_endpoint` | `MetricsEndpoint` | `/actuator/metrics` endpoint for metrics data |
| `prometheus_endpoint` | `PrometheusEndpoint` | `/actuator/prometheus` endpoint for Prometheus scraping |

### Configuration

```yaml
pyfly:
  web:
    actuator:
      enabled: true   # Default: false
```

When enabled, the auto-configuration registers the `ActuatorRegistry`, `HealthAggregator`, and (if `prometheus_client` is installed) the metrics-related endpoints. The web adapter then discovers these beans and mounts the actuator routes automatically.

### Overriding Auto-Configured Beans

You can provide your own `ActuatorRegistry` or `HealthAggregator` via `@configuration` + `@bean`, and the auto-configured versions will be skipped:

```python
from pyfly.container.bean import bean
from pyfly.container import configuration
from pyfly.actuator import ActuatorRegistry, HealthAggregator

@configuration
class MyActuatorConfig:
    @bean
    def health_aggregator(self) -> HealthAggregator:
        agg = HealthAggregator()
        # Add custom indicators...
        return agg
```

**Source:** `src/pyfly/actuator/auto_configuration.py`

---

## Configuration

Actuator-related settings in `pyfly.yaml`:

```yaml
pyfly:
  app:
    name: order-service               # Shown in /actuator/info
    version: 1.0.0                     # Shown in /actuator/info
    description: Order management API  # Shown in /actuator/info

  profiles:
    active: production                 # Shown in /actuator/env

  web:
    actuator:
      enabled: true                    # Enable/disable all actuator endpoints

  actuator:
    endpoints:
      health:
        enabled: true                  # Enabled by default
      beans:
        enabled: true                  # Enabled by default
      env:
        enabled: true                  # Enabled by default
      info:
        enabled: true                  # Enabled by default
      loggers:
        enabled: true                  # Enabled by default
      metrics:
        enabled: false                 # Disabled by default (stub)
```

The framework defaults (from `pyfly-defaults.yaml`):

```yaml
pyfly:
  app:
    name: "pyfly-app"
    version: "0.1.0"
    description: ""
  web:
    actuator:
      enabled: false
```

### Configuration Precedence for Per-endpoint Enable/Disable

1. **Config override (highest):** `pyfly.actuator.endpoints.{endpoint_id}.enabled`
2. **Endpoint default (lowest):** The `enabled` property on the `ActuatorEndpoint`
   implementation

This means you can enable the disabled-by-default `metrics` endpoint or disable any
of the enabled-by-default endpoints without changing code.

---

## Complete Example

The following example shows a full application with custom health indicators, a
custom actuator endpoint, and runtime logger management.

```python
"""order_service/app.py"""

from pyfly.core import pyfly_application, PyFlyApplication
from pyfly.container import component, service, rest_controller
from pyfly.web import request_mapping, get_mapping
from pyfly.web.adapters.starlette import create_app
from pyfly.actuator import ActuatorEndpoint, HealthStatus


# =========================================================================
# Custom Health Indicators
# =========================================================================

@component
class DatabaseHealthIndicator:
    """Contributes database health to /actuator/health."""

    async def health(self) -> HealthStatus:
        try:
            # Replace with actual database connectivity check
            # await db.execute("SELECT 1")
            return HealthStatus(
                status="UP",
                details={
                    "type": "postgresql",
                    "version": "16.1",
                    "pool_active": 3,
                    "pool_max": 20,
                },
            )
        except Exception as e:
            return HealthStatus(
                status="DOWN",
                details={"error": str(e)},
            )


@component
class PaymentServiceHealthIndicator:
    """Contributes payment service health to /actuator/health."""

    async def health(self) -> HealthStatus:
        try:
            # Replace with actual HTTP health check
            # response = await httpx.get("https://payments.example.com/health")
            return HealthStatus(
                status="UP",
                details={"provider": "stripe", "response_time_ms": 45},
            )
        except Exception as e:
            return HealthStatus(
                status="DOWN",
                details={"error": str(e)},
            )


# =========================================================================
# Custom Actuator Endpoint
# =========================================================================

@component
class GitInfoEndpoint:
    """Exposes build/git information at /actuator/git."""

    @property
    def endpoint_id(self) -> str:
        return "git"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context=None) -> dict:
        return {
            "branch": "main",
            "commit": {
                "id": "5c6f83b",
                "time": "2026-02-15T08:30:00Z",
            },
            "build": {
                "version": "1.0.0",
                "timestamp": "2026-02-15T09:00:00Z",
            },
        }


# =========================================================================
# Service and Controller
# =========================================================================

@service
class OrderService:
    async def list_orders(self) -> list[dict]:
        return [
            {"id": "ord-001", "status": "shipped"},
            {"id": "ord-002", "status": "processing"},
        ]


@rest_controller
@request_mapping("/api/orders")
class OrderController:
    def __init__(self, order_service: OrderService) -> None:
        self._service = order_service

    @get_mapping("")
    async def list_orders(self) -> list[dict]:
        return await self._service.list_orders()


# =========================================================================
# Application
# =========================================================================

@pyfly_application(
    name="order-service",
    version="1.0.0",
    scan_packages=["order_service"],
    description="Order management microservice",
)
class Application:
    pass


async def main():
    pyfly_app = PyFlyApplication(Application)
    await pyfly_app.startup()

    app = create_app(
        title="Order Service",
        version="1.0.0",
        context=pyfly_app.context,
        actuator_enabled=True,  # Enables all actuator endpoints
    )

    # The following endpoints are now available:
    #
    # GET  /actuator          -- HAL-style index of all enabled endpoints
    # GET  /actuator/health   -- aggregated health from both indicators
    # GET  /actuator/beans    -- all registered beans
    # GET  /actuator/env      -- active profiles
    # GET  /actuator/info     -- app name, version, description
    # GET  /actuator/loggers         -- list all loggers and their levels
    # POST /actuator/loggers/{name}  -- change a logger's level at runtime
    # GET  /actuator/git             -- custom endpoint: git/build info

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Configuration file (`pyfly.yaml`):**

```yaml
pyfly:
  app:
    name: order-service
    version: 1.0.0
    description: Order management microservice

  profiles:
    active: production

  web:
    actuator:
      enabled: true

  actuator:
    endpoints:
      metrics:
        enabled: true    # Opt in to the metrics stub
```

**Testing the actuator endpoints with `curl`:**

```bash
# Index -- discover all enabled endpoints
curl http://localhost:8080/actuator
# {
#   "_links": {
#     "self": {"href": "/actuator"},
#     "health": {"href": "/actuator/health"},
#     "beans": {"href": "/actuator/beans"},
#     "env": {"href": "/actuator/env"},
#     "info": {"href": "/actuator/info"},
#     "loggers": {"href": "/actuator/loggers"},
#     "metrics": {"href": "/actuator/metrics"},
#     "git": {"href": "/actuator/git"}
#   }
# }

# Health check
curl http://localhost:8080/actuator/health
# {
#   "status": "UP",
#   "components": {
#     "DatabaseHealthIndicator": {
#       "status": "UP",
#       "details": {"type": "postgresql", "version": "16.1", ...}
#     },
#     "PaymentServiceHealthIndicator": {
#       "status": "UP",
#       "details": {"provider": "stripe", "response_time_ms": 45}
#     }
#   }
# }

# Bean registry
curl http://localhost:8080/actuator/beans
# {
#   "beans": {
#     "DatabaseHealthIndicator": {"type": "...", "scope": "SINGLETON", "stereotype": "component"},
#     "PaymentServiceHealthIndicator": {"type": "...", "scope": "SINGLETON", "stereotype": "component"},
#     "GitInfoEndpoint": {"type": "...", "scope": "SINGLETON", "stereotype": "component"},
#     "OrderService": {"type": "...", "scope": "SINGLETON", "stereotype": "service"},
#     "OrderController": {"type": "...", "scope": "SINGLETON", "stereotype": "rest_controller"}
#   }
# }

# Environment
curl http://localhost:8080/actuator/env
# {"activeProfiles": ["production"]}

# Application info
curl http://localhost:8080/actuator/info
# {"app": {"name": "order-service", "version": "1.0.0", "description": "Order management microservice"}}

# List loggers
curl http://localhost:8080/actuator/loggers
# {
#   "loggers": {
#     "ROOT": {"configuredLevel": "INFO", "effectiveLevel": "INFO"},
#     "pyfly.web": {"configuredLevel": null, "effectiveLevel": "INFO"},
#     ...
#   },
#   "levels": ["OFF", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"],
#   "groups": {"web": {...}, "sql": {...}}
# }

# Change a logger's level at runtime (logger name is a path parameter)
curl -X POST http://localhost:8080/actuator/loggers/pyfly.web \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": "DEBUG"}'
# (204 No Content)

# Custom git info endpoint
curl http://localhost:8080/actuator/git
# {"branch": "main", "commit": {"id": "5c6f83b", "time": "..."}, "build": {...}}

# Metrics stub (only available if enabled in config)
curl http://localhost:8080/actuator/metrics
# {"names": [], "message": "Metrics endpoint stub. Configure a metrics backend to populate."}
```

**Kubernetes liveness and readiness probes:**

```yaml
apiVersion: v1
kind: Pod
spec:
  containers:
    - name: order-service
      image: order-service:1.0.0
      livenessProbe:
        httpGet:
          path: /actuator/health
          port: 8080
        initialDelaySeconds: 10
        periodSeconds: 30
      readinessProbe:
        httpGet:
          path: /actuator/health
          port: 8080
        initialDelaySeconds: 5
        periodSeconds: 10
```
