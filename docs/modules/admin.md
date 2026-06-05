# Admin Dashboard Guide

PyFly's admin module provides a Spring Boot Admin-inspired embedded management
dashboard for monitoring and introspecting PyFly applications at runtime. It
ships as a zero-build vanilla JavaScript frontend served directly from the
`pyfly.admin` package, with a JSON REST API and real-time Server-Sent Event
(SSE) streams backing every view. The module follows the same hexagonal
architecture as the rest of the framework: data providers are injected via the
DI container, the Starlette adapter mounts routes, and an `AdminViewExtension`
protocol allows you to plug in custom views without touching framework code.

The admin dashboard requires no additional dependencies beyond the `web` extra
(`starlette`). Enable it in configuration and navigate to `/admin`.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture Overview](#architecture-overview)
3. [Configuration Reference](#configuration-reference)
   - [AdminProperties](#adminproperties)
   - [AdminServerProperties](#adminserverproperties)
   - [AdminClientProperties](#adminclientproperties)
4. [Built-in Views](#built-in-views)
5. [Real-Time Updates (SSE)](#real-time-updates-sse)
6. [REST API Reference](#rest-api-reference)
7. [Extensibility: Custom Views](#extensibility-custom-views)
8. [Server Mode](#server-mode)
   - [Instance Registry](#instance-registry)
   - [Static Discovery](#static-discovery)
9. [Client Registration](#client-registration)
10. [Security](#security)
11. [Auto-Configuration](#auto-configuration)
12. [Spring Boot Admin Comparison](#spring-boot-admin-comparison)

---

## Quick Start

**1. Install PyFly with the web extra** (admin has no separate extra):

```bash
uv add "pyfly[web]"
```

**2. Enable the admin dashboard** in `pyfly.yaml`:

```yaml
pyfly:
  admin:
    enabled: true
```

**3. Run your application:**

```bash
pyfly run
```

**4. Navigate to the dashboard:**

```
http://localhost:8080/admin
```

That is all. The dashboard auto-discovers beans, health indicators, loggers,
scheduled tasks, HTTP mappings, caches, CQRS handlers, transactions, and
metrics from the running `ApplicationContext` and presents them in 15 built-in
views with real-time updates.

---

## Architecture Overview

The admin module is structured into four layers:

```
+--------------------------------------------------------------+
|                       FRONTEND (SPA)                          |
|                                                               |
|  Vanilla JS + CSS served from pyfly.admin.static              |
|  Hash-based client-side routing (#health, #beans, ...)        |
|  SSE subscriptions for real-time updates                      |
|                                                               |
+-------------------------------+------------------------------+
                                | fetches
+-------------------------------+------------------------------+
|                    REST API LAYER                              |
|                                                               |
|  AdminRouteBuilder (Starlette adapter)                        |
|  /admin/api/overview, /admin/api/beans, ...                   |
|  /admin/api/sse/health, /admin/api/sse/metrics, ...           |
|                                                               |
+-------------------------------+------------------------------+
                                | delegates to
+-------------------------------+------------------------------+
|                  DATA PROVIDERS                                |
|                                                               |
|  OverviewProvider    BeansProvider     HealthProvider           |
|  EnvProvider         ConfigProvider    LoggersProvider          |
|  MetricsProvider     ScheduledProvider MappingsProvider         |
|  CacheProvider       CqrsProvider     TransactionsProvider     |
|  TracesProvider      LogfileProvider                            |
|                                                               |
+-------------------------------+------------------------------+
                                | reads from
+-------------------------------+------------------------------+
|              APPLICATION CONTEXT + DI CONTAINER                |
|                                                               |
|  Bean registry, HealthAggregator, CacheManager,               |
|  CommandBus, QueryBus, SagaRegistry, TccRegistry,             |
|  TaskScheduler, ControllerRegistrar, Config                   |
|                                                               |
+--------------------------------------------------------------+
```

**Key components:**

- **AdminRouteBuilder** (`pyfly.admin.adapters.starlette`) -- Builds all
  Starlette `Route` and `Mount` objects for the API, SSE, static files, and
  SPA catch-all.
- **Data Providers** (`pyfly.admin.providers.*`) -- Each provider is a simple
  class that queries the `ApplicationContext` or a specific subsystem and
  returns a plain `dict` serializable to JSON.
- **AdminViewRegistry** (`pyfly.admin.registry`) -- Collects built-in and
  custom `AdminViewExtension` implementations for the views API endpoint.
- **TraceCollectorFilter** (`pyfly.admin.middleware.trace_collector`) -- A
  `OncePerRequestFilter` that captures HTTP request/response metadata into a
  ring buffer (500 entries) for the traces view. Automatically excludes
  `/admin/*` and `/actuator/*` paths.
- **AdminLogHandler** (`pyfly.admin.log_handler`) -- A `logging.Handler`
  subclass that captures log records into a ring buffer (2000 entries) for the
  log viewer. Strips ANSI escape codes and parses structlog `ConsoleRenderer`
  output to extract event names and key-value context.
- **AdminAutoConfiguration** (`pyfly.admin.auto_configuration`) -- Discovered
  via the `pyfly.auto_configuration` entry-point group; registers all admin
  beans when `pyfly.admin.enabled=true` and Starlette is available.

---

## Configuration Reference

All admin configuration lives under the `pyfly.admin.*` namespace in
`pyfly.yaml`.

### AdminProperties

Prefix: `pyfly.admin`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | `bool` | `true` | Enable or disable the admin dashboard. When `false`, no routes are mounted. |
| `path` | `str` | `"/admin"` | Base URL path for the dashboard and API. |
| `title` | `str` | `"PyFly Admin"` | Title displayed in the sidebar header and browser tab. |
| `theme` | `str` | `"auto"` | Color theme: `"auto"`, `"light"`, or `"dark"`. `"auto"` follows the OS preference. |
| `require_auth` | `bool` | `false` | When `true`, the dashboard requires authentication. |
| `allowed_roles` | `list[str]` | `["ADMIN"]` | Roles permitted to access the dashboard when `require_auth` is enabled. |
| `refresh_interval` | `int` | `5000` | SSE and auto-refresh interval in milliseconds. |

### AdminServerProperties

Prefix: `pyfly.admin.server`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | `bool` | `false` | Enable server mode for multi-instance fleet monitoring. |
| `poll_interval` | `int` | `10000` | How often (ms) to poll registered instances for health status. |
| `connect_timeout` | `int` | `2000` | Connection timeout (ms) when reaching out to instances. |
| `read_timeout` | `int` | `5000` | Read timeout (ms) when fetching instance data. |
| `instances` | `list[dict]` | `[]` | Static list of instances for `StaticDiscovery`. Each dict requires `name` and `url` keys, and optionally `metadata`. |

### AdminClientProperties

Prefix: `pyfly.admin.client`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `url` | `str` | `""` | URL of the admin server to register with (e.g. `http://admin-host:8080`). Empty means client registration is disabled. |
| `auto_register` | `bool` | `false` | Automatically register this application with the admin server on startup. |

### Example Configuration

```yaml
pyfly:
  admin:
    enabled: true
    path: /admin
    title: "My Service Admin"
    theme: dark
    require_auth: true
    allowed_roles:
      - ADMIN
      - OPS
    refresh_interval: 3000

    server:
      enabled: true
      poll_interval: 15000
      instances:
        - name: order-service
          url: http://orders:8080
        - name: payment-service
          url: http://payments:8080

    client:
      url: http://admin-server:8080
      auto_register: true
```

---

## Built-in Views

The dashboard ships with 15 views organized into four sections in the sidebar.
Each view has a corresponding REST API endpoint and (where applicable) an SSE
stream for live updates.

### Dashboard

| View | Sidebar ID | Description |
|------|-----------|-------------|
| **Overview** | `(root)` | Aggregated summary: application info, uptime, health status, bean counts by stereotype, and wiring statistics. |
| **Health** | `health` | Health indicator status with component breakdown. Color-coded badges (UP, DOWN, UNKNOWN). Real-time SSE updates. |

### Application

| View | Sidebar ID | Description |
|------|-----------|-------------|
| **Beans** | `beans` | Lists all registered DI beans with stereotype, scope, class name, and inferred category. Beans without an explicit stereotype are classified by class name suffix (e.g., Adapter, Provider, Filter) instead of "none". Click a bean for dependency detail. |
| **Environment** | `env` | Active profiles, system properties, and environment variables (sensitive values masked). |
| **Configuration** | `config` | Resolved configuration tree for all namespaces (not just `pyfly.*`) with source tracking. User-defined config namespaces are included. |
| **Loggers** | `loggers` | Lists all loggers with current log levels and inferred descriptions. Allows runtime log level changes via POST with re-fetch verification. Accepts `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, and `OFF`; `TRACE` maps to numeric level 5 and `OFF` disables the logger (both now apply rather than silently failing). Reset button returns loggers to NOTSET (inherit from parent). |

### Monitoring

| View | Sidebar ID | Description |
|------|-----------|-------------|
| **Metrics** | `metrics` | Built-in process metrics (CPU, memory, threads, GC, uptime) always available without external dependencies. Optional Prometheus metrics included when `prometheus_client` is installed. Selecting a numeric metric opens a **live time-series trend** — a rolling chart polled at the configured refresh interval with a Value / Rate (Δ/s) toggle, pause/resume, Current/Min/Max/Avg summary, a measurement selector for multi-series (tagged) metrics, and a live-refreshing measurements table. Non-numeric metrics show a snapshot instead. |
| **Scheduled Tasks** | `scheduled` | All `@scheduled` tasks with cron expressions, fixed-rate/delay configuration, and execution status. |
| **HTTP Traces** | `traces` | Recent HTTP request/response traces captured by `TraceCollectorFilter`. **Live request analytics**: total requests, average/max latency, error rate (4xx+5xx), a status-code mix bar (2xx/3xx/4xx/5xx), a latency-distribution histogram and latency percentiles (p50/p90/p95/p99) — all updating live as requests arrive. Shows method, path, status code, duration, query string, client host, content type, user agent, and response content-length. Click-to-detail panel. Status code filter pills (All, 2xx, 3xx, 4xx, 5xx). Real-time SSE for new traces. Client buffer bounded to the 500-entry ring buffer. |

### Infrastructure

| View | Sidebar ID | Description |
|------|-----------|-------------|
| **Mappings** | `mappings` | All registered HTTP route mappings with methods, paths, handler references, parameters (with types and path/query/body kind), return type, docstring, and response model. Click-to-detail panel. Method breakdown stat cards. |
| **Caches** | `caches` | Cache adapter type, entry count, key listing with search, per-key eviction, and bulk evict-all. Introspects `InMemoryCache` and `RedisCacheAdapter` via duck-typed `get_stats()` / `get_keys()`. |
| **CQRS** | `cqrs` | Registered command and query handlers with bus pipeline introspection (validation, authorization, metrics, event publishing). |
| **Transactions** | `transactions` | Saga definitions with step DAGs, TCC transactions with participant phase coverage, and in-flight execution count. |
| **Log Viewer** | `logfile` | Real-time log viewer with SSE live tail, level-based color-coded badges (ERROR red, WARNING yellow, INFO blue, DEBUG grey), filter toolbar (All / ERROR / WARNING / INFO / DEBUG), search, pause/resume streaming, clear log buffer, and auto-scroll. Structlog `ConsoleRenderer` output is parsed to extract event names and key-value context; ANSI escape codes are stripped. Ring buffer of 2000 records. |

### Fleet (Server Mode Only)

| View | Sidebar ID | Description |
|------|-----------|-------------|
| **Instances** | `instances` | Fleet view showing all registered application instances with status, URL, and last health check timestamp. Only visible when `pyfly.admin.server.enabled=true`. |

---

## Real-Time Updates (SSE)

The admin dashboard uses Server-Sent Events for live data streaming. The
following SSE endpoints are available:

| Endpoint | Event Name | Behavior |
|----------|-----------|----------|
| `GET /admin/api/sse/health` | `health` | Emits a health status event whenever the aggregate status changes. Poll interval is `refresh_interval / 1000` seconds. |
| `GET /admin/api/sse/metrics` | `metrics` | Emits the full list of metric names at each interval. |
| `GET /admin/api/sse/traces` | `trace` | Emits individual new HTTP trace events as they are captured by the `TraceCollectorFilter`. Polled every 2 seconds. |
| `GET /admin/api/sse/logfile` | `log` | Emits new log records captured by the `AdminLogHandler`. Uses incremental polling (records with id > last seen) every 1 second. Each event contains `id`, `timestamp`, `level`, `logger`, `message`, `context`, and `thread`. |
| `GET /admin/api/sse/beans` | `beans` | Emits the bean registry snapshot at each interval (poll interval is `refresh_interval / 1000` seconds) so the Beans view stays live. |

Each SSE stream sends JSON payloads in the standard `data:` format with
appropriate `Cache-Control: no-cache` and `X-Accel-Buffering: no` headers for
proxy compatibility.

### Connecting from JavaScript

```javascript
const source = new EventSource('/admin/api/sse/health');
source.addEventListener('health', (event) => {
    const data = JSON.parse(event.data);
    console.log('Health status:', data.status);
});
```

---

## REST API Reference

All API endpoints return JSON responses. The base path defaults to `/admin/api`
(configurable via `pyfly.admin.path`).

### Data Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/api/overview` | Aggregated application overview. |
| `GET` | `/admin/api/beans` | List all registered beans. |
| `GET` | `/admin/api/beans/{name}` | Bean detail by name. Returns 404 if not found. |
| `GET` | `/admin/api/health` | Health status. Returns HTTP 503 when status is DOWN. |
| `GET` | `/admin/api/env` | Environment and profiles. |
| `GET` | `/admin/api/config` | Resolved configuration tree. |
| `GET` | `/admin/api/loggers` | List all loggers with levels. |
| `POST` | `/admin/api/loggers/{name}` | Set logger level. Body: `{"level": "DEBUG"}`. Valid levels: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, `OFF` (an unknown level returns HTTP 400). `TRACE` and `OFF` are applied as numeric level 5 and a disabled logger respectively. |
| `GET` | `/admin/api/metrics` | List metric names. |
| `GET` | `/admin/api/metrics/{name}` | Metric detail by name. |
| `GET` | `/admin/api/scheduled` | List scheduled tasks. |
| `GET` | `/admin/api/mappings` | List HTTP route mappings. |
| `GET` | `/admin/api/caches` | Cache stats: adapter type, entry count, key list. |
| `GET` | `/admin/api/caches/keys` | List all cache keys (filtered from stats). |
| `POST` | `/admin/api/caches/{name}/evict` | Evict a cache key. Body: `{"key": "specific-key"}`. Omit `key` to evict all. |
| `GET` | `/admin/api/cqrs` | List CQRS command/query handlers and bus pipeline status. |
| `GET` | `/admin/api/transactions` | List saga and TCC definitions with in-flight count. |
| `GET` | `/admin/api/traces` | List HTTP traces. Optional query param: `?limit=100`. |
| `GET` | `/admin/api/logfile` | Log records from in-memory ring buffer. Returns `{ available, records, total }`. |
| `POST` | `/admin/api/logfile/clear` | Clear all log records from the buffer. Returns `{ cleared: true }`. |
| `GET` | `/admin/api/views` | List registered view extensions (built-in + custom). |
| `GET` | `/admin/api/settings` | Dashboard settings (title, theme, refresh interval, server mode flag). |

### SSE Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/api/sse/health` | Real-time health status stream. |
| `GET` | `/admin/api/sse/metrics` | Real-time metrics stream. |
| `GET` | `/admin/api/sse/traces` | Real-time HTTP trace stream. |
| `GET` | `/admin/api/sse/logfile` | Real-time log record stream. |
| `GET` | `/admin/api/sse/beans` | Real-time bean registry stream. |

### Instance Registry Endpoints (Server Mode)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/api/instances` | List all registered instances. |
| `POST` | `/admin/api/instances` | Register an instance. Body: `{"name": "...", "url": "...", "metadata": {}}`. |
| `DELETE` | `/admin/api/instances/{name}` | Deregister an instance by name. |

---

## Extensibility: Custom Views

You can add custom views to the dashboard by implementing the
`AdminViewExtension` protocol and registering the implementation as a DI
component.

### AdminViewExtension Protocol

```python
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class AdminViewExtension(Protocol):
    @property
    def view_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def icon(self) -> str: ...

    async def get_data(self, context: Any = None) -> dict[str, Any]: ...
```

| Property / Method | Description |
|-------------------|-------------|
| `view_id` | Unique identifier for the view (used in the sidebar navigation and API). |
| `display_name` | Human-readable name shown in the sidebar. |
| `icon` | Icon identifier (matches the Feather icon set used in the sidebar). |
| `get_data()` | Async method that returns the data payload for the view. Called by the `/admin/api/views` endpoint. |

### Example: Custom Deployment View

```python
from pyfly.container import component
from pyfly.admin import AdminViewExtension

@component
class DeploymentView:
    """Custom admin view showing deployment information."""

    @property
    def view_id(self) -> str:
        return "deployments"

    @property
    def display_name(self) -> str:
        return "Deployments"

    @property
    def icon(self) -> str:
        return "upload-cloud"

    async def get_data(self, context=None) -> dict:
        return {
            "last_deploy": "2026-02-17T12:00:00Z",
            "version": "1.2.3",
            "environment": "production",
        }
```

The `AdminViewRegistry` auto-discovers all beans implementing
`AdminViewExtension` from the `ApplicationContext` during startup.

---

## Server Mode

Server mode transforms the admin dashboard into a central fleet monitoring
console. When enabled, it adds an `InstanceRegistry` that tracks remote
application instances, a `StaticDiscovery` strategy for configuration-based
instance lists, and API endpoints for dynamic registration and deregistration.

### Enabling Server Mode

```yaml
pyfly:
  admin:
    enabled: true
    server:
      enabled: true
      poll_interval: 10000
      instances:
        - name: order-service
          url: http://orders:8080
        - name: payment-service
          url: http://payments:8080
          metadata:
            region: us-east-1
```

### Instance Registry

`InstanceRegistry` maintains an in-memory registry of known application
instances. Each instance is represented by an `InstanceInfo` dataclass:

```python
@dataclass
class InstanceInfo:
    name: str
    url: str
    status: str = "UNKNOWN"        # UP, DOWN, UNKNOWN
    last_checked: datetime | None = None
    metadata: dict = field(default_factory=dict)
```

The registry supports:

| Method | Description |
|--------|-------------|
| `register(name, url, metadata)` | Add or overwrite an instance. |
| `deregister(name)` | Remove an instance by name. |
| `get_instances()` | List all registered instances. |
| `get_instance(name)` | Look up a single instance. |
| `update_status(name, status)` | Update status and `last_checked` timestamp. |

### Static Discovery

`StaticDiscovery` reads the `pyfly.admin.server.instances` configuration list
and registers each entry with the `InstanceRegistry` at startup. Each entry
requires `name` and `url` keys:

```yaml
instances:
  - name: app-1
    url: http://localhost:8080
  - name: app-2
    url: http://localhost:8081
```

---

## Client Registration

Applications can auto-register with a remote admin server using
`AdminClientRegistration`. This is the counterpart to server mode -- the
*client* application announces itself to the *server* application's instance
registry.

### Configuration

```yaml
pyfly:
  admin:
    enabled: true
    client:
      url: http://admin-server:8080
      auto_register: true
```

When `pyfly.admin.client.url` is set, the `AdminAutoConfiguration` creates an
`AdminClientRegistration` bean. This bean provides `register()` and
`deregister()` async methods that POST/DELETE to the admin server's
`/admin/api/instances` endpoint, plus `start()`/`stop()` lifecycle hooks driven
by the `ApplicationContext`.

### How It Works

- When `auto_register` is `true`, the `ApplicationContext` calls `start()` on
  startup (which registers the app) and `stop()` on shutdown (which deregisters
  it). `register()` swallows its own errors, so an unreachable admin server
  never aborts application startup.
- `register()` sends a POST with `{"name": "<app-name>", "url": "<app-url>"}`
  to the admin server. The application name and URL are read from
  `pyfly.app.name` and `pyfly.app.url` configuration.
- `deregister()` sends a DELETE to `/admin/api/instances/{name}`.
- HTTP requests use `httpx` when available; otherwise fall back to
  `urllib.request` (blocking, run in the default executor).
- Both methods return `True` on success, `False` on failure (with logging).

---

## Security

The admin dashboard supports authentication gating via two configuration
properties:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.admin.require_auth` | `bool` | `false` | When `true`, the dashboard and all API endpoints require an authenticated user. |
| `pyfly.admin.allowed_roles` | `list[str]` | `["ADMIN"]` | Roles that are permitted to access the admin dashboard. Only evaluated when `require_auth` is enabled. |

When `require_auth` is `true`, the guard is enforced on **every**
`{base}/api/*` route — data, mutation, SSE, and instance-registry endpoints
alike. The guard reads the request-scoped `SecurityContext` (populated by the
security `WebFilter` chain via `RequestContext`):

- Unauthenticated requests receive a `401` with `{"error": "Authentication required"}`.
- Authenticated users missing **every** role in `allowed_roles` receive a `403`
  with `{"error": "Forbidden"}`.

The SPA shell (`index.html`) and static assets stay public so the dashboard can
still boot and then surface the API's `401`/`403` responses in the UI. When
`require_auth` is `false` (the default) the guard is a no-op and all API routes
are open.

### Example: Restrict to OPS and ADMIN Roles

```yaml
pyfly:
  admin:
    enabled: true
    require_auth: true
    allowed_roles:
      - ADMIN
      - OPS
```

---

## Auto-Configuration

Admin auto-configuration is activated when two conditions are met:

1. `pyfly.admin.enabled` is set to `true` in configuration.
2. Starlette is available on the classpath (satisfied by installing the `web` extra).

The `AdminAutoConfiguration` class is discovered via the
`pyfly.auto_configuration` entry-point group in `pyproject.toml`:

```toml
[project.entry-points."pyfly.auto_configuration"]
admin = "pyfly.admin.auto_configuration:AdminAutoConfiguration"
```

### Conditions and Beans

| Condition | Effect |
|-----------|--------|
| `@conditional_on_property("pyfly.admin.enabled", having_value="true")` | Only activates when admin is explicitly enabled. |
| `@conditional_on_class("starlette")` | Requires Starlette to be importable. |

| Bean | Type | Description |
|------|------|-------------|
| `admin_properties` | `AdminProperties` | Bound configuration dataclass. |
| `admin_view_registry` | `AdminViewRegistry` | Collects built-in and custom view extensions. |
| `runtime_provider` | `RuntimeProvider` | Provides runtime metrics (CPU, memory, uptime) for the admin dashboard. |
| `admin_log_handler` | `AdminLogHandler` | In-memory log handler attached to the root logger (ring buffer of 2000). Parses structlog output and strips ANSI codes. |
| `admin_client_registration` | `AdminClientRegistration` | Created only when `pyfly.admin.client.url` is set. |

> **Note:** `TraceCollectorFilter` is **not** a DI bean. It is wired directly into
> the ASGI middleware chain by `create_app()` — this must happen before beans are
> instantiated, so registering it as a bean would not reach the request path.

### Packaging

The admin module has **no separate extra dependency**. It only requires
Starlette, which is already provided by the `web` extra. Static assets (HTML,
CSS, JavaScript) are packaged inside `src/pyfly/admin/static/` and included
automatically by the wheel build target (`packages = ["src/pyfly"]`).

---

## Spring Boot Admin Comparison

| Spring Boot Admin (Java) | PyFly Admin (Python) |
|--------------------------|---------------------|
| `spring-boot-admin-server` | `pyfly.admin` with `server.enabled=true` |
| `spring-boot-admin-client` | `pyfly.admin.client.url` configuration |
| `spring.boot.admin.client.url` | `pyfly.admin.client.url` |
| `spring.boot.admin.ui.title` | `pyfly.admin.title` |
| Vaadin/React frontend | Vanilla JS SPA (zero-build) |
| Eureka / Consul discovery | `StaticDiscovery` (configuration-based) |
| `@EnableAdminServer` | `pyfly.admin.server.enabled: true` |
| `/actuator` endpoints | `/admin/api/*` endpoints + `/actuator` integration |
| SSE/WebSocket notifications | SSE streams (`/admin/api/sse/*`) |
| `spring.boot.admin.ui.theme` | `pyfly.admin.theme` (`auto`, `light`, `dark`) |
| Logfile viewer (via `/actuator/logfile`) | In-memory log viewer with SSE live tail (`/admin/api/logfile` + `/admin/api/sse/logfile`) |
| Cache management (JMX/Actuator) | Cache stats, key listing, per-key eviction (`/admin/api/caches`) |
| Log level management | Runtime log level changes via `/admin/api/loggers/{name}` |
