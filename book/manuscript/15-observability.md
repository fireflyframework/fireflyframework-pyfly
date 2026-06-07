<span class="eyebrow">Chapter 15</span>

# Observability & the Admin Dashboard {.chtitle}

::: figure art/openers/ch15.svg | &nbsp;

In Chapter 13 you surrounded Lumen's hot paths with caches and wrapped outbound calls in circuit breakers. In Chapter 14 you secured every endpoint with JWT authentication, role guards, and server-side sessions.

Lumen is now fast and safe — but it is still a black box. When a wallet deposit takes three seconds in production you need to know *where* those seconds went. When a downstream payment service degrades you need a dashboard that lights up red *before* your on-call engineer gets paged. When a compliance auditor asks why a particular transfer was rejected you need structured log records with enough context to reconstruct the decision.

This chapter adds eyes and ears to Lumen. The three pillars of observability answer three complementary questions about a running system:

| Pillar | Question | PyFly module |
|---|---|---|
| **Logging** | "What happened, and in what context?" | `pyfly.logging` |
| **Metrics** | "How much? How fast? How often?" | `pyfly.observability.metrics` |
| **Tracing** | "What path did this request take?" | `pyfly.observability.tracing` |

On top of those pillars sits the **Actuator** — production management endpoints that expose health, bean wiring, environment, and live logger levels — and the **Admin Dashboard**, an embedded browser UI that ties everything together in a single pane of glass.

Finally, you will see how **Aspect-Oriented Programming** (AOP) applies logging and metrics to every service method declaratively, without touching the methods themselves.

By the end of the chapter Lumen will produce structured JSON logs with correlation IDs and automatic PII masking, emit Prometheus metrics scraped by any standard collector, propagate OpenTelemetry trace spans across service boundaries, answer Kubernetes liveness and readiness probes, and display all of the above in a zero-configuration dashboard reachable at `/admin`.

---

## Structured logging & PII redaction

### Why structured logging?

Traditional log lines look like this:

```
[INFO] Order ord-123 created for customer acme corp (email: sales@acme.com)
```

Searching for `ord-123` in Elasticsearch works — until the format changes. And `sales@acme.com` landing in a log file may violate your data-protection policy without your team even noticing.

**Structured logging** replaces the interpolated string with an event name and explicit key-value pairs. The pairs render as JSON in production and as readable `key=value` in development. A log aggregation system ingests JSON natively; you query on `wallet_id` or `owner_id` as first-class fields, independent of message format.

### get_logger

PyFly exposes a single factory function that returns a structured logger backed by `structlog` (when the `observability` extra is installed) or a zero-dependency stdlib shim otherwise. Both accept the same call signature:

::: listing lumen/logging_demo.py | Listing 15.1 — Structured logger usage
from pyfly.logging import get_logger

logger = get_logger("lumen.wallet")

logger.info("wallet_opened", wallet_id="wlt-001", owner_id="usr-42")
logger.warning("balance_low", wallet_id="wlt-001", remaining=300)
logger.error(
    "deposit_rejected",
    wallet_id="wlt-001",
    reason="insufficient_funds",
)
:::

In development with `format: console` the output reads naturally:

```
10:30:00 [info    ] wallet_opened   wallet_id=wlt-001 owner_id=usr-42
10:30:01 [warning ] balance_low     wallet_id=wlt-001 remaining=300
10:30:02 [error   ] deposit_rejected wallet_id=wlt-001 reason=insufficient_funds
```

In production with `format: json` every line is a self-contained JSON object:

```json
{"event":"wallet_opened","wallet_id":"wlt-001","owner_id":"usr-42",
 "timestamp":"2026-06-07T10:30:00Z","level":"info",
 "logger":"lumen.wallet"}
```

Configure logging in `pyfly.yaml`:

```yaml
pyfly:
  logging:
    level:
      root: INFO
      lumen.wallet: DEBUG
      sqlalchemy.engine: WARNING
    format: console          # console | json | logfmt
```

`level.<name>` overrides the root level for any logger whose name begins with
that prefix. `sqlalchemy.engine: WARNING` silences query logs without touching
your code. An environment variable `PYFLY_LOGGING_LEVEL_ROOT=WARNING` overrides
the config key, which is useful for staging builds.

!!! tip "Why not stdlib `logging` directly?"
    `logging.getLogger("x").info("event", wallet_id="wlt-001")` raises
    `TypeError` — the stdlib rejects keyword arguments. `get_logger` guarantees
    the structured signature works regardless of which adapter is active.

### Correlation IDs

**Correlation IDs** link every log line emitted during a single HTTP request. PyFly binds a `transaction_id` to the current async context automatically through `TransactionIdMiddleware`. Your handlers can bind additional fields — such as the authenticated user — so that those fields flow through all subsequent log calls without being passed explicitly:

::: listing lumen/wallet/handler.py | Listing 15.2 — Binding correlation context
import structlog

from pyfly.logging import get_logger

logger = get_logger("lumen.wallet")


async def handle_deposit(wallet_id: str, amount: int, owner_id: str) -> dict:
    structlog.contextvars.bind_contextvars(
        wallet_id=wallet_id,
        owner_id=owner_id,
    )

    logger.info("deposit_started", amount=amount)
    # ... business logic ...
    result = {"wallet_id": wallet_id, "new_balance": 1350}
    logger.info("deposit_completed", new_balance=result["new_balance"])

    structlog.contextvars.unbind_contextvars("wallet_id", "owner_id")
    return result
:::

Every `logger.*` call inside `handle_deposit` — including calls deep in downstream service methods — automatically carries `wallet_id` and `owner_id` without any further plumbing.

### PII redaction

**PII redaction** is enabled by default. Before any log record reaches an output handler, PyFly scans the rendered message for emails, credit-card numbers, IBANs, SSNs, JWTs, bearer tokens, and URL credentials. Detected patterns are replaced with `<EMAIL>`, `<CREDIT_CARD>`, and so on.

The regex engine ships with every install. The Presidio-backed NER engine — which also catches free-text names and addresses — is available via the `[pii]` extra and activates automatically when installed:

```bash
uv add "pyfly[observability,pii]"
python -m spacy download en_core_web_sm   # lighter model; lg for higher recall
```

Configure redaction in `pyfly.yaml`:

```yaml
pyfly:
  logging:
    redaction:
      enabled: true          # default; set false to disable entirely
      engine: auto           # regex | presidio | auto (presidio if installed)
      mask: placeholder      # placeholder (<EMAIL>) | partial (****@acme.com)
      deny-fields:
        - password
        - token
        - secret
      presidio:
        score-threshold: 0.6
        languages: [en, es]
```

`deny-fields` lists structured log field *names* whose values are unconditionally replaced with `<REDACTED>`. Use it for fields like `password` where you know the value is sensitive without inspecting the content.

!!! spring "Spring parity"
    Spring Boot does not ship built-in PII redaction; teams integrate Logback
    `MaskingMessageConverter` or custom appenders manually. PyFly's redaction
    applies to *all* loggers — including third-party libraries — through a
    single `ProcessorFormatter` / `RedactionFilter` installed on the root
    handler. No per-library configuration is needed.

### Rolling file appender

When logs go to a file rather than stdout, configure rotation in `pyfly.yaml`:

```yaml
pyfly:
  logging:
    file:
      name: lumen.log
      path: ./logs
    rolling:
      max-size: 50MB
      max-history: 14
```

PyFly writes to `./logs/lumen.log` and rotates at 50 MB, keeping 14 rotated files before discarding the oldest. The same PII redaction pass applies to file output.

---

## Metrics

### The MetricsRegistry

**`MetricsRegistry`** is a thin wrapper around `prometheus_client` that guarantees each metric name is registered only once. Duplicate calls to `counter()` or `histogram()` with the same name return the existing metric rather than raising a `ValueError`. Inject it from the DI container (auto-configured when `prometheus_client` is installed) or create it manually:

::: listing lumen/observability/metrics.py | Listing 15.3 — Creating metrics
from pyfly.observability import MetricsRegistry

registry = MetricsRegistry()

deposits_total = registry.counter(
    name="lumen.deposits.total",
    description="Deposit operations completed",
    labels=["status"],
)

deposit_duration = registry.histogram(
    name="lumen.deposits.duration",
    description="Deposit processing time in seconds",
    labels=["status"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
:::

`counter()` and `histogram()` return native `prometheus_client.Counter` and `prometheus_client.Histogram` objects, so every Prometheus ecosystem tool — Grafana dashboards, alerting rules, recording rules — works without modification.

| Method | Returns | Use for |
|---|---|---|
| `registry.counter(name, description, labels)` | `Counter` | Monotonically increasing counts |
| `registry.histogram(name, description, labels, buckets)` | `Histogram` | Durations, sizes, latency percentiles |
| `registry.counter(…)` called again | Same `Counter` | Safe deduplication |

### @timed — automatic duration histogram

**`@timed`** records how long an async or sync function takes to run, using a labeled histogram. It works on any callable and automatically adds `class`, `method`, and `exception` labels:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 15.4 — @timed on DepositFundsHandler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher
from pyfly.observability import MetricsRegistry, timed

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository

registry = MetricsRegistry()


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @timed(registry, "lumen.deposit.duration", "Deposit handler latency")
    @transactional()
    async def do_handle(self, command: DepositFunds) -> int:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

The decorator captures `start = time.perf_counter()`, calls the function, and observes the elapsed time in the `finally` block. The `exception` label is `"none"` on success and the exception type name on failure, so you can split latency by outcome in Grafana. The `class` and `method` labels are derived automatically from the function's qualified name.

Histogram names follow Micrometer dot.case convention: `"lumen.deposit.duration"` becomes `lumen_deposit_duration_seconds` in Prometheus, with a `_seconds` suffix added if absent.

### @counted — automatic invocation counter

**`@counted`** increments a counter on every function call. Lumen's `GetBalanceHandler` is a natural fit — every balance read increments the counter, tagged by outcome:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listing 15.5 — @counted on GetBalanceHandler
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.observability import MetricsRegistry, counted

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository

registry = MetricsRegistry()


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):

    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    @counted(registry, "lumen.balance.reads", "Balance queries served")
    async def do_handle(self, query: GetBalance) -> BalanceDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

On success the counter is incremented with labels `class="GetBalanceHandler"`, `method="do_handle"`, `result="success"`, and `exception="none"`. On failure it uses `result="failure"` and `exception=<TypeName>`, then re-raises the original exception. The counter name receives a `_total` suffix in Prometheus automatically, per the naming convention.

You can stack both decorators on the same method. The following listing shows Lumen's `WithdrawFundsHandler` timed and counted simultaneously:

::: listing lumen/core/services/wallets/withdraw_funds_handler.py | Listing 15.6 — Stacking @timed and @counted
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher
from pyfly.observability import MetricsRegistry, counted, timed

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository

registry = MetricsRegistry()


@command_handler
@service
class WithdrawFundsHandler(CommandHandler[WithdrawFunds, int]):
    """Debit funds from a wallet; returns the new balance in minor units."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @timed(registry, "lumen.withdrawal.duration", "Withdrawal latency")
    @counted(registry, "lumen.withdrawals", "Withdrawal attempts")
    @transactional()
    async def do_handle(self, command: WithdrawFunds) -> int:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet = to_aggregate(entity)
        wallet.withdraw(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

`amount` is an `int` in minor units (e.g. 1050 = €10.50) — the `Money` value object enforces the type. Each invocation produces both a histogram observation and a counter increment.

### Prometheus scrape endpoint

The actuator (covered in the next section) exposes the metrics registry for scraping. When the actuator is enabled and `prometheus_client` is installed, two endpoints are mounted automatically with no additional code:

- `GET /actuator/metrics` — Micrometer-compatible JSON listing all metric names
- `GET /actuator/prometheus` — standard text-exposition format for scraping

Point your Prometheus `scrape_configs` at `/actuator/prometheus` and all `MetricsRegistry` metrics appear alongside built-in process metrics (CPU, memory, threads, GC).

!!! spring "Spring parity"
    `MetricsRegistry` mirrors Spring's `MeterRegistry` from Micrometer.
    `@timed` corresponds to Spring's `@Timed` and `@counted` to `@Counted`.
    Dot.case names (`lumen.deposit.duration`) match the Micrometer convention
    that Spring Boot Actuator's `/actuator/metrics` also exposes.

---

## Distributed tracing

### @span — OpenTelemetry span decorator

**`@span`** wraps an async or sync function in an OpenTelemetry span. Each span is a timed, named unit of work. Spans nest automatically through OpenTelemetry's context propagation, so a `@span`-decorated function called from inside another `@span`-decorated function produces a parent-child relationship in your trace viewer:

::: listing lumen/wallet/service.py | Listing 15.7 — @span on CQRS handler methods
from pyfly.observability import span


class DepositFundsHandler:

    @span("deposit-funds")
    async def do_handle(self, command):
        balance = await self._fetch_wallet(command.wallet_id)
        await self._persist_deposit(command.wallet_id, command.amount)
        return balance + command.amount

    @span("fetch-wallet")
    async def _fetch_wallet(self, wallet_id: str) -> int:
        # ... repository.find(wallet_id) ...
        return 1000

    @span("persist-deposit")
    async def _persist_deposit(self, wallet_id: str, amount: int) -> None:
        # ... repository.add(wallet) ...
        pass
:::

In a trace viewer this appears as:

```
deposit-funds  [120 ms]
  +-- fetch-wallet   [15 ms]
  +-- persist-deposit [90 ms]
```

`@span` creates a tracer named `"pyfly"` via `trace.get_tracer("pyfly")`. When the decorated function raises, the span automatically records the error: it sets status to `ERROR`, calls `current_span.record_exception(exc)`, then re-raises so callers see the original exception unmodified. Sync functions are supported identically — no `await` on the decorated side.

### OpenTelemetry auto-configuration

PyFly wires up a `TracerProvider` with a `BatchSpanProcessor` automatically
when `opentelemetry-api` and `opentelemetry-sdk` are installed:

```bash
uv add opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp
```

Configure the exporter in `pyfly.yaml`:

```yaml
pyfly:
  observability:
    tracing:
      enabled: true
      service-name: "${pyfly.app.name}"
      exporter: otlp
      otlp:
        endpoint: "http://localhost:4318"
```

Exporter selection rules:

- `exporter: otlp` — uses OTLP/HTTP; requires `opentelemetry-exporter-otlp`
- `exporter: console` — prints spans to stdout; useful for local debugging
- `exporter: none` — records spans but discards them (useful in tests)
- Unset — auto-selects OTLP if `pyfly.observability.tracing.otlp.endpoint`
  or the `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable is configured;
  otherwise logs a single info line and discards spans silently

!!! tip "Custom TracerProvider"
    If you need gRPC export or a non-standard SDK configuration, register your
    own `TracerProvider` before PyFly starts — `trace.set_tracer_provider(…)`.
    PyFly detects an existing global provider and skips auto-configuration.

### Context propagation — inbound and outbound

Spans stay correlated *within* a process automatically. Keeping the same trace *across* multiple services requires extracting the upstream trace context from inbound HTTP headers and injecting the current context into every outbound call.

PyFly handles both ends without any per-handler code.

**Inbound — TracingFilter:**

`TracingFilter` is wired into Lumen's filter chain immediately after `CorrelationFilter` by `create_app()`. For each request it reads the W3C `traceparent` header, opens a SERVER span as a child of the upstream context, and keeps that span active for the lifetime of the request. Every `@span` created during the request — and every log line — belongs to the caller's distributed trace:

```python
# Simplified view of what TracingFilter does per-request:
parent = extract_context(request.headers)   # parse W3C traceparent
with tracer.start_as_current_span(
    f"{request.method} {request.url.path}",
    context=parent,
    kind=trace.SpanKind.SERVER,
) as span:
    response = await call_next(request)
    span.set_attribute("http.response.status_code", response.status_code)
```

When OpenTelemetry is not installed, the filter is a transparent pass-through.

**Outbound — HttpxClientAdapter:**

`HttpxClientAdapter` calls `inject_headers()` on every outbound request so downstream services can continue the same trace:

::: listing lumen/client/inventory_client.py | Listing 15.8 — Trace propagation
from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter


class InventoryClient:

    def __init__(self) -> None:
        self._http = HttpxClientAdapter(
            base_url="http://inventory-service:8080"
        )

    async def check_stock(self, sku: str) -> dict:
        # The active traceparent is injected automatically into
        # the outbound request headers — no manual plumbing required.
        resp = await self._http.request("GET", f"/skus/{sku}")
        return resp.json()
:::

**Logs carry trace_id and span_id:**

`StructlogAdapter` registers a processor that stamps the active span's IDs on every log record. No code change required — any `get_logger(…)` call inside an active span gains `trace_id` and `span_id` fields automatically:

```json
{
  "event": "deposit_completed",
  "wallet_id": "wlt-001",
  "new_balance": 1350,
  "trace_id": "1a4b3145ed8f2dd11172ee3584123f4a",
  "span_id": "d2a62aaa81b0ad66",
  "timestamp": "2026-06-07T10:30:00Z",
  "level": "info",
  "logger": "lumen.wallet"
}
```

With `trace_id` in every log record you can jump from a Grafana Loki search for `wallet_id=wlt-001` directly to the correlated Tempo trace view, and from there to the Prometheus latency charts for that time window — all three pillars joined on a single identifier.

The low-level propagation helpers are available if you ever need them directly:

```python
from pyfly.observability.propagation import (
    extract_context,   # parse traceparent from inbound headers
    inject_headers,    # write traceparent into outbound headers
    current_trace_ids, # -> (trace_id, span_id) hex, or None
    has_otel,          # True if opentelemetry is importable
)
```

---

## Health checks & the Actuator

::: figure art/figures/15-observability.svg | Figure 15.1 — The Actuator exposes health, beans, loggers, and Prometheus metrics over HTTP. Kubernetes liveness and readiness probes hit the dedicated sub-paths.

The **Actuator** gives Kubernetes and your ops tooling a stable contract: a set of management endpoints that expose health, bean wiring, environment state, and metric scrapers. You configure it once and every tool from `kubectl` to Grafana can consume it without custom code.

### Enabling the Actuator

Pass `actuator_enabled=True` to `create_app()`, or set the flag in `pyfly.yaml`:

```yaml
pyfly:
  web:
    actuator:
      enabled: true
  app:
    name: lumen
    version: 1.0.0
    description: Lumen wallet service
```

When enabled, `create_app()` automatically scans the DI container for `HealthIndicator` beans, creates a `HealthAggregator`, instantiates all built-in endpoints, and mounts them at `/actuator/*`.

### Built-in endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/actuator` | GET | HAL-style index of all enabled endpoints |
| `/actuator/health` | GET | Aggregated status: `UP` (200) or `DOWN` (503) |
| `/actuator/health/liveness` | GET | Kubernetes liveness probe sub-path |
| `/actuator/health/readiness` | GET | Kubernetes readiness probe sub-path |
| `/actuator/beans` | GET | All registered DI beans with stereotype and scope |
| `/actuator/env` | GET | Active configuration profiles |
| `/actuator/info` | GET | Application name, version, description |
| `/actuator/loggers` | GET, POST | List loggers; change levels at runtime |
| `/actuator/metrics` | GET | Micrometer-compatible JSON metric names |
| `/actuator/prometheus` | GET | Prometheus text-exposition scrape target |
| `/actuator/threaddump` | GET | Snapshot of all live threads and stack traces |
| `/actuator/refresh` | POST | Evict refresh-scoped beans; re-bind config |

### Custom HealthIndicator

Any `@component` bean with an `async def health(self) -> HealthStatus` method is automatically discovered and registered as a health indicator. Lumen's `WalletRepository` is a good candidate — `count()` issues a lightweight `SELECT COUNT(*)` against the live database session without mutating any data:

::: listing lumen/health/indicators.py | Listing 15.9 — HealthIndicator beans
from pyfly.actuator import HealthStatus
from pyfly.container import component

from lumen.models.repositories.wallet_repository import WalletRepository


@component
class WalletRepositoryHealthIndicator:
    """Checks the wallet store is reachable with a lightweight probe."""

    def __init__(self, repository: WalletRepository) -> None:
        self._repository = repository

    async def health(self) -> HealthStatus:
        try:
            # count() issues SELECT COUNT(*) — fast, read-only probe.
            total = await self._repository.count()
            return HealthStatus(
                status="UP",
                details={"store": "wallet-repository", "rows": total},
            )
        except Exception as exc:
            return HealthStatus(
                status="DOWN",
                details={"error": str(exc)},
            )


@component
class DatabaseHealthIndicator:
    """Checks database connectivity via a lightweight SELECT 1."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def health(self) -> HealthStatus:
        try:
            async with self._factory() as session:
                await session.execute("SELECT 1")
            return HealthStatus(
                status="UP",
                details={"type": "postgresql", "pool_active": 3},
            )
        except Exception as exc:
            return HealthStatus(
                status="DOWN",
                details={"error": str(exc)},
            )
:::

`HealthStatus.status` accepts four values: `"UP"`, `"DOWN"`, `"OUT_OF_SERVICE"`, or `"UNKNOWN"`. The aggregator applies a severity ordering (`DOWN > OUT_OF_SERVICE > UP > UNKNOWN`) and returns the worst-case status across all indicators. If any indicator's `health()` method raises, that indicator is treated as `"DOWN"` with `details={"error": "check failed"}`; the exception is logged but does not crash the health endpoint.

A healthy response looks like:

```json
{
  "status": "UP",
  "components": {
    "WalletRepositoryHealthIndicator": {
      "status": "UP",
      "details": {"store": "wallet-repository", "rows": 42}
    },
    "DatabaseHealthIndicator": {
      "status": "UP",
      "details": {"type": "postgresql", "pool_active": 3}
    }
  }
}
```

### Changing log levels at runtime

The loggers endpoint lets you inspect and change log levels without restarting Lumen — invaluable when a production incident needs DEBUG output for exactly one package:

```bash
# List all loggers with configured and effective levels
curl http://localhost:8080/actuator/loggers

# Enable DEBUG for the wallet module — takes effect immediately
curl -X POST http://localhost:8080/actuator/loggers/lumen.wallet \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": "DEBUG"}'

# Reset to inherit from parent
curl -X POST http://localhost:8080/actuator/loggers/lumen.wallet \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": null}'
```

The endpoint uses Spring Boot's level vocabulary (`OFF`, `ERROR`, `WARN`, `INFO`, `DEBUG`, `TRACE`) and is drop-in compatible with Spring Boot Actuator tooling.

### Custom actuator endpoint

To expose a custom endpoint, implement the `ActuatorEndpoint` protocol and annotate the class with `@component`. PyFly discovers it during context startup and mounts it at `/actuator/{endpoint_id}` automatically:

::: listing lumen/actuator/git_info.py | Listing 15.10 — Custom actuator endpoint
from pyfly.container import component


@component
class GitInfoEndpoint:
    """Exposes build metadata at /actuator/git."""

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
                "time": "2026-06-07T08:30:00Z",
            },
            "build": {
                "version": "1.0.0",
            },
        }
:::

### Kubernetes probe configuration

Point your pod spec at the dedicated liveness and readiness sub-paths so Kubernetes can make independent restart and traffic decisions:

```yaml
livenessProbe:
  httpGet:
    path: /actuator/health/liveness
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 30
readinessProbe:
  httpGet:
    path: /actuator/health/readiness
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
```

The separate sub-paths let you group indicators independently — an in-flight migration that degrades readiness need not trigger a liveness restart and container recreation.

!!! spring "Spring parity"
    PyFly's Actuator mirrors Spring Boot Actuator. `HealthIndicator`,
    `HealthStatus`, `HealthAggregator`, `ActuatorEndpoint`, and
    `ActuatorRegistry` correspond directly to their Spring counterparts.
    The loggers endpoint uses the same Spring Boot level vocabulary and
    `configuredLevel`/`effectiveLevel` response shape, making it compatible
    with Spring Boot Admin and Actuator-aware tooling out of the box.
    `MetricsAutoConfiguration` and `MetricsActuatorAutoConfiguration`
    mirror Spring Boot's Micrometer auto-configuration: when
    `prometheus_client` is installed, `/actuator/prometheus` appears without
    any manual wiring.

---

## The Admin Dashboard

The **Admin Dashboard** is a zero-build, zero-dependency browser UI served directly from the `pyfly.admin` package. One configuration line enables it; navigate to `/admin` — no separate server, no `npm` build step.

### Enabling the dashboard

```yaml
pyfly:
  admin:
    enabled: true
    title: "Lumen Admin"
    theme: auto           # auto | light | dark
    refresh_interval: 5000
```

The dashboard auto-discovers beans, health indicators, loggers, scheduled tasks, HTTP mappings, caches, CQRS handlers, sagas, and metrics from the running `ApplicationContext`. It presents them in **15 built-in views** with real-time Server-Sent Event (SSE) updates — no WebSocket, no polling loop in your code.

### Built-in views

**Dashboard section:**

| View | Description |
|---|---|
| Overview | App info, uptime, health badge, bean counts by stereotype |
| Health | Component status with color-coded UP / DOWN / UNKNOWN badges; live SSE |

**Application section:**

| View | Description |
|---|---|
| Beans | All DI beans with stereotype, scope, and dependency graph |
| Environment | Active profiles and masked environment variables |
| Configuration | Resolved config tree for all namespaces with source tracking |
| Loggers | Logger levels with runtime level-change UI; TRACE and OFF supported |

**Monitoring section:**

| View | Description |
|---|---|
| Metrics | CPU, memory, threads, GC, uptime; optional Prometheus metrics; live trend chart |
| Scheduled Tasks | All `@scheduled` tasks with cron expressions and status |
| HTTP Traces | Request/response traces with p50/p90/p95/p99 latency, error-rate bar |
| Log Viewer | Live-tail with level filters, search, and pause/resume |

**Infrastructure section:**

| View | Description |
|---|---|
| Mappings | All HTTP routes with handler, parameters, return type, and docstring |
| Caches | Adapter type, entry count, per-key eviction, bulk evict-all |
| CQRS | Command and query handlers with bus pipeline introspection |
| Transactions | Saga step DAGs and TCC participant coverage; in-flight count |

**Fleet section (server mode):**

| View | Description |
|---|---|
| Instances | All registered remote application instances with health status |

### Real-time SSE streams

The dashboard never polls the backend with `setInterval`. It opens a single `EventSource` connection and the server pushes events as they occur:

| SSE endpoint | Event name | What it streams |
|---|---|---|
| `/admin/api/sse/health` | `health` | Status change whenever aggregate health changes |
| `/admin/api/sse/metrics` | `metrics` | Full metric-name list at each refresh interval |
| `/admin/api/sse/traces` | `trace` | Individual HTTP traces as they arrive |
| `/admin/api/sse/logfile` | `log` | New log records from the in-memory ring buffer |
| `/admin/api/sse/beans` | `beans` | Bean registry snapshot at each refresh interval |

The log viewer ring buffer holds 2,000 records; the HTTP traces ring buffer holds 500. Admin and actuator paths (`/admin/*`, `/actuator/*`) are excluded from trace capture automatically so they do not pollute the trace panel.

### Runtime logger management

The Loggers view uses the same `/admin/api/loggers/{name}` endpoint as the actuator. Click a logger row to open an inline level selector and submit — the new level takes effect immediately, and the UI re-fetches to confirm the change. The Reset button sends `null` to return the logger to `NOTSET` (inherit from parent).

### Custom view extension

To add your own sidebar view, implement `AdminViewExtension` and annotate with `@component`. The dashboard discovers it at startup:

::: listing lumen/admin/deployment_view.py | Listing 15.11 — Custom admin view
from pyfly.container import component


@component
class DeploymentView:
    """Shows deployment metadata in the admin sidebar."""

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
            "last_deploy": "2026-06-07T08:00:00Z",
            "version": "1.0.0",
            "environment": "production",
        }
:::

`view_id` sets the sidebar URL fragment (`#deployments`), `display_name` appears in the sidebar menu, and `icon` maps to a Feather icon. `get_data()` is called by `GET /admin/api/views` and can query the DI container, a database, or any external source.

### Security

Restrict dashboard access to operators in production:

```yaml
pyfly:
  admin:
    enabled: true
    require_auth: true
    allowed_roles:
      - ADMIN
      - OPS
```

When `require_auth: true`, every `/admin/api/*` route — data, mutation, SSE, and instance-registry endpoints — requires an authenticated principal whose roles overlap with `allowed_roles`. Unauthenticated requests receive `401`; authenticated users who lack every listed role receive `403`. The static SPA shell remains public so the dashboard can boot and display the error message.

### Fleet monitoring — server mode

For a fleet of Lumen instances, run one dedicated admin-server and point every application instance at it:

```yaml
# Admin server instance
pyfly:
  admin:
    enabled: true
    server:
      enabled: true
      poll_interval: 10000
      instances:
        - name: lumen-1
          url: http://lumen-1:8080
        - name: lumen-2
          url: http://lumen-2:8080
```

```yaml
# Each application instance
pyfly:
  admin:
    enabled: true
    client:
      url: http://admin-server:8080
      auto_register: true
```

`StaticDiscovery` seeds the registry from the YAML list. `AdminClientRegistration` registers the instance on startup and deregisters on shutdown. HTTP calls use `httpx` when available and fall back to `urllib.request`; registration errors are silently swallowed so an unreachable admin server never aborts application startup.

!!! spring "Spring parity"
    PyFly Admin maps directly to Spring Boot Admin. `server.enabled: true`
    replaces `@EnableAdminServer`. `client.url` replaces
    `spring.boot.admin.client.url`. The Vaadin/React frontend is replaced with
    a vanilla-JS SPA that requires no build tooling. SSE streams replace
    Spring Boot Admin's WebSocket notifications. The built-in Log Viewer
    replaces the Spring Boot Admin logfile viewer backed by `/actuator/logfile`;
    PyFly's ring buffer approach avoids the file-path configuration that
    Spring Boot Admin requires.

---

## AOP for cross-cutting concerns

### What is AOP?

**Aspect-Oriented Programming** separates cross-cutting concerns — logging, metrics, security, auditing — from business logic. Without AOP, every service method begins with `logger.info(...)` and ends with `metrics.increment(...)`. With AOP, you write that logic once in an `@aspect` class and apply it to every matching method via a pointcut expression — the methods themselves stay clean.

PyFly's AOP module ships five advice types:

| Advice | Decorator | Runs |
|---|---|---|
| Before | `@before` | Before the target method |
| After returning | `@after_returning` | After the method succeeds |
| After throwing | `@after_throwing` | After the method raises |
| After (finally) | `@after` | Always, success or failure |
| Around | `@around` | Wraps the entire call; must call `jp.proceed()` |

### @aspect — declaring an aspect

**`@aspect`** marks a class as a PyFly aspect. The class is automatically registered in the DI container as a singleton and receives injected dependencies via `__init__`. No explicit base class is required:

::: listing lumen/aspects/logging_aspect.py | Listing 15.12 — A logging aspect
from pyfly.aop import aspect, before, after_returning, after_throwing, JoinPoint
from pyfly.container.ordering import order
from pyfly.logging import get_logger

logger = get_logger("lumen.audit")


@aspect
@order(-50)
class AuditLoggingAspect:
    """Logs entry, exit, and failure for every service method."""

    @before("service.*.*")
    def log_entry(self, jp: JoinPoint) -> None:
        logger.info(
            "method_called",
            cls=type(jp.target).__name__,
            method=jp.method_name,
        )

    @after_returning("service.*.*")
    def log_return(self, jp: JoinPoint) -> None:
        logger.info(
            "method_returned",
            cls=type(jp.target).__name__,
            method=jp.method_name,
        )

    @after_throwing("service.*.*")
    def log_error(self, jp: JoinPoint) -> None:
        logger.error(
            "method_raised",
            cls=type(jp.target).__name__,
            method=jp.method_name,
            exc=type(jp.exception).__name__,
        )
:::

The pointcut `"service.*.*"` matches every public method on every `@service`-stereotype bean. `*` matches exactly one dot-separated segment; `**` matches one or more. Partial globs are supported within a segment: `"service.*.do_handle"` matches all `do_handle` methods on all service-stereotype handlers.

Qualified names follow the pattern `"{stereotype}.{ClassName}.{method_name}"`, so `service.DepositFundsHandler.do_handle` uniquely identifies the `do_handle` method on `DepositFundsHandler`.

!!! tip "`@before` handlers must be synchronous"
    `@before`, `@after_returning`, `@after_throwing`, and `@after` handlers
    are always called synchronously by the weaver. Only `@around` handlers
    can be async (and must `await jp.proceed()` when advising an async method).

### @around — metrics without decorators

**`@around`** is the most powerful advice type. It wraps the entire method execution; call `await jp.proceed()` to invoke the original method (or the next advice in the chain) and add behaviour on either side:

::: listing lumen/aspects/metrics_aspect.py | Listing 15.13 — @around metrics aspect
import time

from pyfly.aop import JoinPoint, around, aspect
from pyfly.container.ordering import order
from pyfly.observability import MetricsRegistry

registry = MetricsRegistry()


@aspect
@order(50)
class MetricsAspect:
    """Records duration and call counts for every service method."""

    @around("service.*.*")
    async def record_metrics(self, jp: JoinPoint):
        start = time.perf_counter()
        exc_name = "none"
        try:
            result = await jp.proceed()
            return result
        except Exception as exc:
            exc_name = type(exc).__name__
            raise
        finally:
            elapsed = time.perf_counter() - start
            histogram = registry.histogram(
                f"service.{jp.method_name}.duration",
                f"Duration of {jp.method_name}",
                labels=["exception"],
            )
            histogram.labels(exception=exc_name).observe(elapsed)
:::

`@order(-50)` on `AuditLoggingAspect` and `@order(50)` on `MetricsAspect` ensure the logging aspect fires first in the advice chain. `HIGHEST_PRECEDENCE = -(2^31)` runs earliest; `LOWEST_PRECEDENCE = 2^31 - 1` runs last.

### Automatic weaving — AspectBeanPostProcessor

In production you never call `weave_bean()` manually. `AopAutoConfiguration` registers **`AspectBeanPostProcessor`** unconditionally. During context startup the post-processor:

1. Collects every bean whose class has `__pyfly_aspect__ = True` into an `AspectRegistry`.
2. For every non-aspect bean, checks whether any registered pointcut matches any public method.
3. Wraps matching methods in-place with the full advice chain via `weave_bean()`.

The result is zero-configuration AOP: define aspects, define services, start the application — the weaver wires them together.

### JoinPoint reference

Every advice handler receives a `JoinPoint` dataclass:

| Attribute | Available in | Description |
|---|---|---|
| `target` | All | The bean instance being intercepted |
| `method_name` | All | Name of the intercepted method |
| `args` | All | Positional arguments passed to the method |
| `kwargs` | All | Keyword arguments passed to the method |
| `return_value` | `@after_returning`, `@after` | Return value (after success) |
| `exception` | `@after_throwing`, `@after` | The raised exception (or `None`) |
| `proceed` | `@around` only | Callable; `await`-able for async methods |

### Putting it together — full observability on DepositFundsHandler

Lumen's deposit handler with all three observability pillars applied — zero observability code inside the business logic:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 15.14 — DepositFundsHandler with full observability
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher
from pyfly.logging import get_logger
from pyfly.observability import MetricsRegistry, counted, span, timed

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository

logger = get_logger("lumen.wallet")
registry = MetricsRegistry()


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """
    Credits funds to an existing wallet and returns the new balance
    (in minor units, e.g. 1350 = €13.50).

    Logging, metrics, and tracing are applied by decorators and aspects;
    the business logic here stays free of cross-cutting concerns.
    """

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @timed(registry, "lumen.wallet.deposit.duration", "Deposit latency")
    @counted(registry, "lumen.wallet.deposits", "Total deposit attempts")
    @span("wallet-deposit")
    @transactional()
    async def do_handle(self, command: DepositFunds) -> int:
        logger.info("deposit_started", wallet_id=command.wallet_id,
                    amount=command.amount)
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        logger.info("deposit_completed", wallet_id=command.wallet_id,
                    new_balance=wallet.balance.amount)
        return wallet.balance.amount
:::

**How it works.** `@span` opens an OpenTelemetry span. `@timed` records the `do_handle` duration. `@counted` increments the call counter. `AuditLoggingAspect` fires `@before` and `@after_returning` on every service method. `MetricsAspect` adds its own `@around` histogram. PII redaction strips sensitive values from log output automatically. The actuator exposes `/actuator/health`, `/actuator/prometheus`, and `/actuator/loggers`. The Admin Dashboard shows live traces and log records.

`command.amount` is an `int` in minor units — enforced by the `DepositFunds` command's validator (`amount > 0`). The `Money` value object wraps it with the wallet's `Currency`, preventing cross-currency arithmetic at the domain boundary.

Seven lines of decorators and one `get_logger` call — and Lumen's deposit path is fully observable.

---

## What you built {.recap}

You started with a production-ready but opaque service. By the end of this
chapter Lumen:

- Emits **structured JSON logs** with correlation IDs, async-context-bound
  fields, and automatic PII redaction via regex and optional Presidio NER.
- Exports **Prometheus metrics** from `MetricsRegistry`, tagged with
  `@timed` duration histograms and `@counted` invocation counters, scraped
  at `/actuator/prometheus`.
- Propagates **OpenTelemetry traces** end-to-end: `TracingFilter` opens a
  SERVER span from the inbound `traceparent` header, `@span` creates child
  spans for handler calls, `HttpxClientAdapter` injects context into outbound
  requests, and `StructlogAdapter` stamps every log record with `trace_id`
  and `span_id`.
- Answers **Kubernetes health probes** at `/actuator/health/liveness` and
  `/actuator/health/readiness` via auto-discovered `HealthIndicator` beans,
  including a `WalletRepositoryHealthIndicator` that probes the wallet store.
- Surfaces all of the above in the **embedded Admin Dashboard** at `/admin`,
  with 15 built-in views, real-time SSE streams, a live log tail, an HTTP
  trace analytics panel, and runtime logger-level management.
- Applies logging and metrics **cross-cuttingly** via `@aspect`,
  `@before`, `@after_returning`, `@after_throwing`, and `@around`, woven
  automatically by `AspectBeanPostProcessor` without touching handler code.

## Try it yourself {.exercises}

1. **PII redaction audit.** Add a log statement to `DepositFundsHandler.do_handle`
   that includes a fake email address as a field value
   (`customer_email="alice@example.com"`) and a `token` field with an
   arbitrary string. Run Lumen locally with `format: console`, observe that
   the email is replaced with `<EMAIL>` and the token field value is replaced
   with `<REDACTED>`. Then switch `engine: presidio` (after installing
   `pyfly[pii]` and `en_core_web_sm`) and compare the output.

2. **Custom HealthIndicator.** Write a `StripeHealthIndicator` that calls
   `https://status.stripe.com/api/v2/status.json` with `httpx`, parses
   `indicator.status`, and returns `UP` if the value is `"none"` or `DOWN`
   otherwise. Register it as a `@component` and verify that
   `/actuator/health` includes a `StripeHealthIndicator` component. Test
   with a mocked `httpx` call that raises `httpx.ConnectError` and verify
   the aggregated status becomes `DOWN`.

3. **Metrics aspect with per-method thresholds.** Extend `MetricsAspect`
   from Listing 15.13 with a configurable `slow_threshold` (default 0.5 s)
   injected from `pyfly.yaml` via `@config_properties`. When a service
   method exceeds the threshold, emit a `logger.warning("slow_method", …)`
   with `method_name` and `elapsed` fields. Write a pytest test that uses
   a `FakeClock` or `unittest.mock.patch("time.perf_counter")` to simulate
   a slow call and asserts the warning is logged.
