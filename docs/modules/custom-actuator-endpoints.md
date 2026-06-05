# Custom Actuator Endpoints Guide

PyFly's actuator module is fully extensible. You can create custom management
endpoints that are auto-discovered from the DI container and exposed alongside the
built-in health, beans, env, info, loggers, metrics, prometheus, and threaddump
endpoints.

---

## Table of Contents

1. [ActuatorEndpoint Protocol](#actuatorendpoint-protocol)
2. [Creating a Custom Endpoint](#creating-a-custom-endpoint)
3. [Auto-Discovery](#auto-discovery)
4. [Per-Endpoint Configuration](#per-endpoint-configuration)
5. [ActuatorRegistry](#actuatorregistry)
6. [Built-in Endpoints Reference](#built-in-endpoints-reference)
   - [Health](#health)
   - [Beans](#beans)
   - [Environment](#environment)
   - [Info](#info)
   - [Loggers](#loggers)
   - [Metrics](#metrics)
   - [Prometheus](#prometheus)
   - [Thread Dump](#thread-dump)
7. [Index Endpoint](#index-endpoint)
8. [Complete Example](#complete-example)

---

## ActuatorEndpoint Protocol

Every actuator endpoint implements the `ActuatorEndpoint` protocol from
`pyfly.actuator.ports`:

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

| Property/Method | Description |
|---|---|
| `endpoint_id` | URL suffix — the endpoint is served at `/actuator/{endpoint_id}` |
| `enabled` | Default enable state. Can be overridden per-endpoint in config. |
| `handle(context)` | Returns a JSON-serializable dict for the response body. |

**Source:** `src/pyfly/actuator/ports.py`

---

## Creating a Custom Endpoint

Implement the `ActuatorEndpoint` protocol and decorate with `@component` for
auto-discovery:

```python
from pyfly.container import component


@component
class GitInfoEndpoint:
    """Exposes Git commit info at /actuator/git."""

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
                "id": "abc1234",
                "message": "feat: add git info endpoint",
                "time": "2026-02-15T10:30:00Z",
            },
        }
```

This endpoint will be available at `GET /actuator/git` and will appear in the
`/actuator` index endpoint's `_links`.

---

## Auto-Discovery

The `ActuatorRegistry.discover_from_context(context)` method scans all beans in the
DI container for instances implementing the `ActuatorEndpoint` protocol:

```python
# In create_app() when actuator_enabled=True:
registry = ActuatorRegistry(config=config)

# Register built-in endpoints
registry.register(HealthEndpoint(agg))
registry.register(BeansEndpoint(context))
# ...

# Auto-discover custom ActuatorEndpoint beans
registry.discover_from_context(context)
```

Any `@component` (or `@service`, `@repository`, etc.) that satisfies the
`ActuatorEndpoint` protocol is automatically registered. No additional configuration
is needed.

---

## Per-Endpoint Configuration

Each endpoint's enable state can be overridden in `pyfly.yaml`:

```yaml
pyfly:
  actuator:
    endpoints:
      health:
        enabled: true       # Keep health enabled (default)
      loggers:
        enabled: false      # Disable loggers in production
      git:
        enabled: true       # Enable custom git endpoint
      metrics:
        enabled: true       # Auto-enabled when prometheus_client is installed
```

The config key pattern is: `pyfly.actuator.endpoints.{endpoint_id}.enabled`

**Priority order:**
1. Config override (highest priority)
2. Endpoint's own `enabled` property (default)

---

## ActuatorRegistry

The `ActuatorRegistry` (`pyfly.actuator.registry`) manages all endpoint instances:

```python
from pyfly.actuator import ActuatorRegistry

registry = ActuatorRegistry(config=config)

# Register endpoints
registry.register(my_endpoint)

# Get only enabled endpoints
enabled = registry.get_enabled_endpoints()
# Returns: dict[str, ActuatorEndpoint]

# Auto-discover from DI context
registry.discover_from_context(application_context)
```

| Method | Description |
|---|---|
| `register(endpoint)` | Register an `ActuatorEndpoint` instance. |
| `get_enabled_endpoints()` | Return all endpoints whose enable state is `True`. |
| `discover_from_context(context)` | Scan DI container for `ActuatorEndpoint` beans. |

**Source:** `src/pyfly/actuator/registry.py`

---

## Built-in Endpoints Reference

### Health

| Property | Value |
|---|---|
| Path | `/actuator/health` |
| Methods | GET |
| Default | Enabled |
| Special | Returns 200 (UP) or 503 (DOWN) |

Aggregates all `HealthIndicator` beans. See the [Actuator Guide](actuator.md) for details.

### Beans

| Property | Value |
|---|---|
| Path | `/actuator/beans` |
| Methods | GET |
| Default | Enabled |

Lists all registered beans with type, scope, and stereotype.

### Environment

| Property | Value |
|---|---|
| Path | `/actuator/env` |
| Methods | GET |
| Default | Enabled |

Returns active configuration profiles.

### Info

| Property | Value |
|---|---|
| Path | `/actuator/info` |
| Methods | GET |
| Default | Enabled |

Returns application metadata from `pyfly.app.*` config.

### Loggers

| Property | Value |
|---|---|
| Path | `/actuator/loggers` |
| Methods | GET, POST |
| Default | Enabled |

**GET:** Lists all loggers with configured and effective levels, plus built-in
logger groups. Levels use Spring Boot's vocabulary.

```json
{
    "loggers": {
        "ROOT": {"configuredLevel": "INFO", "effectiveLevel": "INFO"},
        "pyfly.web": {"configuredLevel": null, "effectiveLevel": "INFO"}
    },
    "levels": ["OFF", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"],
    "groups": {
        "web": {"configuredLevel": null, "members": ["pyfly.web", "uvicorn", "starlette"]},
        "sql": {"configuredLevel": null, "members": ["sqlalchemy", "pyfly.data"]}
    }
}
```

**POST `/actuator/loggers/{name}`:** Changes a logger's level at runtime. The
logger name is a path parameter; the body carries `configuredLevel` (send `null`
to reset to inherited). Returns `204 No Content` on success.

```bash
curl -X POST http://localhost:8080/actuator/loggers/pyfly.web \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": "DEBUG"}'
# (204 No Content)
```

**Source:** `src/pyfly/actuator/endpoints/loggers_endpoint.py`

### Metrics

| Property | Value |
|---|---|
| Path | `/actuator/metrics` and `/actuator/metrics/{name}` |
| Methods | GET |
| Default | Enabled when `prometheus_client` installed |

Exposes Prometheus registry data in Micrometer-compatible JSON (dot-case names,
`COUNT`/`TOTAL_TIME`/`MAX`/`VALUE` statistics). Auto-configured by
`MetricsActuatorAutoConfiguration` when `prometheus_client` is importable.

**Source:** `src/pyfly/actuator/endpoints/metrics_endpoint.py`

### Prometheus

| Property | Value |
|---|---|
| Path | `/actuator/prometheus` |
| Methods | GET |
| Default | Enabled when `prometheus_client` installed |

Prometheus text exposition format (`text/plain; version=0.0.4`) scrape target.
Auto-configured alongside the metrics endpoint.

**Source:** `src/pyfly/actuator/endpoints/prometheus_endpoint.py`

### Thread Dump

| Property | Value |
|---|---|
| Path | `/actuator/threaddump` |
| Methods | GET |
| Default | Enabled |

Returns all live threads with their stack traces. `className` is the Python module
name; `methodName` uses `co_qualname` (Python 3.11+) to include the enclosing
class, falling back to `co_name`.

**Source:** `src/pyfly/actuator/endpoints/threaddump_endpoint.py`

---

## Index Endpoint

`GET /actuator` returns a HAL-style index listing all enabled endpoints:

```json
{
    "_links": {
        "self": {"href": "/actuator"},
        "health": {"href": "/actuator/health"},
        "beans": {"href": "/actuator/beans"},
        "env": {"href": "/actuator/env"},
        "info": {"href": "/actuator/info"},
        "loggers": {"href": "/actuator/loggers"},
        "git": {"href": "/actuator/git"}
    }
}
```

This index is automatically generated from the registry's enabled endpoints.

---

## Complete Example

```python
"""app.py — Application with custom actuator endpoints."""

from pyfly.container import component
from pyfly.core import pyfly_application, PyFlyApplication
from pyfly.web.adapters.starlette import create_app
from pyfly.actuator import HealthStatus


# ── Custom Actuator Endpoint ──────────────────────────────

@component
class CacheStatsEndpoint:
    """Exposes cache statistics at /actuator/cache-stats."""

    @property
    def endpoint_id(self) -> str:
        return "cache-stats"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context=None) -> dict:
        return {
            "hits": 1234,
            "misses": 56,
            "hit_rate": 0.956,
            "evictions": 12,
        }


@component
class FeatureFlagsEndpoint:
    """Exposes feature flag state at /actuator/features."""

    @property
    def endpoint_id(self) -> str:
        return "features"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context=None) -> dict:
        return {
            "flags": {
                "dark-mode": True,
                "beta-checkout": False,
                "new-search": True,
            }
        }


# ── Custom Health Indicator ───────────────────────────────

@component
class DatabaseHealthIndicator:
    async def health(self) -> HealthStatus:
        return HealthStatus(status="UP", details={"type": "postgresql"})


# ── Application ───────────────────────────────────────────

@pyfly_application(
    name="my-service",
    version="1.0.0",
    scan_packages=["app"],
)
class Application:
    pass


async def main():
    pyfly_app = PyFlyApplication(Application)
    await pyfly_app.startup()

    app = create_app(
        title="My Service",
        version="1.0.0",
        context=pyfly_app.context,
        actuator_enabled=True,
    )

    # Available endpoints:
    # GET /actuator              — index with _links
    # GET /actuator/health       — aggregated health
    # GET /actuator/beans        — bean registry
    # GET /actuator/env          — active profiles
    # GET /actuator/info         — app metadata
    # GET /actuator/loggers      — logger config
    # GET /actuator/cache-stats  — custom: cache statistics
    # GET /actuator/features     — custom: feature flags

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Configuration to disable a custom endpoint:**

```yaml
pyfly:
  actuator:
    endpoints:
      cache-stats:
        enabled: false  # Disable cache stats in production
```
