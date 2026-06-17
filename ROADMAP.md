# Roadmap

PyFly's roadmap is driven by achieving feature parity with the full [Firefly Framework](https://github.com/fireflyframework) Java ecosystem (40+ Spring Boot modules). This document tracks which modules are planned and their priority.

---

## Current State (v26.06.103)

PyFly ships with **39 fully-implemented modules** covering the foundation, application, infrastructure, integration, and cross-cutting layers — including the rewritten transactional engine (Saga + Workflow + TCC), Event Sourcing, IDP, ECM, Notifications, Webhooks, Callbacks, Plugins, Rule Engine, Config Server, and the **`pyfly.domain` DDD primitives** (`v26.05.02`). The starter system reached Java/.NET parity in `v26.05.03`: declarative `@enable_*_stack` decorators now actually activate the bundle's property defaults at boot, an imperative `register_*_stack(app)` API mirrors .NET's `services.AddFireflyXxx(...)`, and a new `@enable_web_stack` ships dedicated web-tier wiring. See the [Changelog](CHANGELOG.md) for full details.

Phases 1, 2, and 3 of the original roadmap landed in `v26.05.01`. The **DDD starters** portion of Phase 4 landed in `v26.05.02`, and the **layered-bundle starter system** reached parity with the Java and .NET ports in `v26.05.03`. A subsequent production-readiness and parity-hardening wave (`v26.06.78` → `v26.06.93`) replaced mock-only coverage with real-backend integration tests, fixed production blockers, and brought the weaker subsystems to full hexagonal parity — details in the subsection below. Backoffice and Utils remain planned.

### Production-readiness & parity hardening (v26.06.78 → v26.06.93)

This wave replaced mock-only adapter coverage with provable real-backend correctness and closed the remaining parity gaps across every major subsystem.

**Integration-test foundation.** A `testcontainers`-backed integration suite (`pytest -m integration`) exercises every adapter against a real backend (Postgres, MySQL, MongoDB, Redis, Kafka, RabbitMQ). A `@requires_docker` marker skips cleanly when Docker is absent; `PYFLY_INTEGRATION_REQUIRE_DOCKER=1` turns skips into hard failures for CI. The fast unit suite remains the merge gate; the integration suite runs nightly and on manual dispatch.

**Real-backend adapter hardening.**
- *MongoDB production fix:* `beanie>=2.1` dropped Motor; the production client was switched to `pymongo.AsyncMongoClient` (fixing a crash against any real MongoDB server). The `@transactional` MongoDB arm received a matching fix.
- *PostgreSQL cache adapter:* `PostgresCacheAdapter` — a durable SQL cache (`pyfly_cache_entries`, BYTEA value, `TIMESTAMPTZ` expiry, `ON CONFLICT` upsert, LIKE-prefix evict) — brings the cache layer to Java parity.
- *RabbitMQ EDA bus:* `RabbitMqEventBus` (aio-pika) joins Kafka, Redis Streams, and Postgres as a fully-tested, at-least-once event-bus option.
- *Durable orchestration & event-sourcing persistence:* orchestration state is now config-selectable (`memory | redis | sqlalchemy | cache`); event and snapshot stores gain SQLAlchemy backends and a config-selectable provider. An `EventSourcingPublisher` bridges stored events onto the EDA bus.

**Subsystem depth.**
- *HTTP:* An `IdempotencyWebFilter` (opt-in via `pyfly.web.idempotency.enabled`) replays stored responses for repeat mutating requests carrying the same `Idempotency-Key`. Exception converters added for SQLAlchemy `IntegrityError` → 409, httpx errors → 502/504, and open circuit breaker → 503.
- *CQRS:* `EdaCacheInvalidationBridge` now correctly evicts cached query results in response to domain events; `@publish_domain_event(destination=...)` is wired end-to-end.
- *Client:* Real-transport tests for every protocol client (HTTP, GraphQL, SOAP, WebSocket, gRPC). New `pyfly[grpc]` and `pyfly[websocket]` extras; `GrpcClientBuilder` and `WebSocketClientBuilder` auto-wire as beans.
- *Notifications:* `Jinja2TemplateEngine` for local template rendering; per-recipient opt-out (`NotificationPreferenceService`) checked across all `to`/`cc`/`bcc`/push addresses; `pyfly_notifications_*` metrics.
- *Security / IDP:* `IdpAdapter` port extended with `get_user_info`, `register_user`, `get_roles`, and TOTP MFA (`mfa_challenge`/`mfa_verify`). `InternalDbIdpAdapter` fully implements MFA via pyotp; new per-provider extras (`idp-cognito`, `idp-azure`, `idp-keycloak`).
- *Resilience:* `ResilienceRegistry` materializes named `CircuitBreaker`/`RateLimiter`/`Bulkhead`/`TimeLimiter` instances from `pyfly.resilience.*` config keys (previously dead code).
- *Callbacks/Webhooks:* Outbound callbacks now use a real `httpx`-backed sender (previously no-op). Redis-backed `RedisWebhookEventStore` for distributed dedup. New signature validators: `StripeSignatureValidator`, `GitHubSignatureValidator`, `TwilioSignatureValidator`.
- *Config Server:* `GitConfigBackend` clones and serves config from a Git repository (new `pyfly[config-server-git]` extra). Tiered `search_locations` overlay merges config across multiple base directories. Backend-selection config keys are now wired (were previously dead code).
- *Plugins:* `PluginState` lifecycle model (LOADED/STARTED/STOPPED/FAILED), per-plugin start/stop with dependency cascade, typed `PluginException` hierarchy.
- *Rule Engine:* Rich operator set (`between`, `contains`, `starts_with`, `ends_with`, `exists`, `is_null`, `is_empty`); fluent builder DSL (`pyfly.rule_engine.builder`); `RuleSetLoader.from_json`; `RuleSetValidator`; hexagonal `RuleEnginePort` + `ActionHandler` SPI; `RuleEngineService` facade; `EvaluationMode.ALL`/`FIRST_MATCH`; pluggable action handlers.

### Server-layer observability ✅ **Delivered (v26.06.113)**

Observability was previously application-layer only (the `MetricsFilter` `http_server_requests_seconds`, tracing/correlation filters, `process_metrics`). This release adds metrics about the ASGI **server** itself (uvicorn, granian, hypercorn), written to the Prometheus registry and auto-exposed at `/actuator/prometheus` and `/actuator/metrics`. Every meter is labeled `server` (server type) and `worker_pid`.

Three cooperating mechanisms supply the data. A pure-ASGI `ServerMetricsASGIMiddleware` wraps the app at the outermost layer and is the primary source — it runs in every worker for every server and worker count, emitting `server_active_connections`, `server_in_flight_requests`, and `server_requests_total`. A `ServerMetricsBinder`, started from the in-worker ASGI lifespan, emits `server_workers`, `server_uptime_seconds` (since this worker bound, distinct from `process_uptime_seconds`), `server_started_total`, `server_stopped_total`, and optionally `server_native_connections`. A best-effort `ServerStatsPort` lets each adapter enrich the data on the in-process `serve_async` path — the uvicorn adapter surfaces its true socket connection count (incl. idle keep-alive) and total requests via `server_native_connections`; granian/hypercorn report workers + uptime only.

Multi-worker scrapes aggregate: `pyfly run` sets `PROMETHEUS_MULTIPROC_DIR` before forking workers, and `/actuator/prometheus` merges all workers via `MultiProcessCollector`, so the `server_*` and `http_server_requests_*` meters reflect every worker (custom Python collectors such as `process_*`/`system_*` are not aggregated by multiprocess mode).

New config keys: `pyfly.server.observability.enabled` (default `true`; enabled by the web and core starters), `pyfly.server.observability.sample-interval-seconds` (default `5.0`), and `pyfly.server.observability.access-log` (default `false`). Requires the observability extra (`prometheus_client`); degrades to a no-op without it. The admin dashboard gains a live **Observability** section under Monitoring — stat cards, rolling charts, and a per-worker breakdown table — backed by `GET /admin/api/observability` and SSE `/admin/api/sse/observability`. gunicorn is not added in this release (the stack stays async-only ASGI: granian > uvicorn > hypercorn), but the `ServerStatsPort` + multiprocess design is gunicorn-ready. The local `docker-compose.yml` gained prometheus + grafana services scraping `/actuator/prometheus`.

---

## Phase 1 — Core Distributed Patterns ✅ **Complete (v26.05.01)**

| Module | Description | Java Source | Status |
|--------|-------------|-------------|--------|
| **Saga / Transactions** | Distributed Saga orchestration with compensation, DAG topology, retries, idempotency, recovery | [`fireflyframework-transactional-engine`](https://github.com/fireflyframework/fireflyframework-transactional-engine) | Done (rewritten in v26.05.01) |
| **TCC** | Try / Confirm / Cancel three-phase transactions with `Annotated[T, FromTry()]` propagation | [`fireflyframework-transactional-engine`](https://github.com/fireflyframework/fireflyframework-transactional-engine) | Done in v26.05.01 |
| **Workflow** | Durable, signal-driven orchestration: `@wait_for_signal`/`@wait_for_timer`/child workflows/queries/cron | [`fireflyframework-workflow`](https://github.com/fireflyframework/fireflyframework-workflow) | Done in v26.05.01 |
| **Event Sourcing** | `AggregateRoot`, `EventStore`, snapshots, transactional outbox, projections, upcasting | [`fireflyframework-eventsourcing`](https://github.com/fireflyframework/fireflyframework-eventsourcing) | Done in v26.05.01 |

---

## Phase 2 — Business Logic ✅ **Complete (v26.05.01)**

| Module | Description | Java Source | Status |
|--------|-------------|-------------|--------|
| **Rule Engine** | YAML/JSON DSL + fluent builder; rich operators; AST evaluation with `FIRST_MATCH`/`ALL` modes; hexagonal port + service; pluggable action handlers; batch evaluation; validation; metrics | [`fireflyframework-rule-engine`](https://github.com/fireflyframework/fireflyframework-rule-engine) | Done — hardened in v26.06.93 |
| **Plugins** | Plugin SPI: `@plugin` / `@extension_point` / `@extension`, dependency-ordered lifecycle | [`fireflyframework-plugins`](https://github.com/fireflyframework/fireflyframework-plugins) | Done in v26.05.01 |
| **Data Processing** | Job orchestration, enrichment pipelines, CQRS integration for batch workloads | [`fireflyframework-data`](https://github.com/fireflyframework/fireflyframework-data) | Covered by `pyfly.data` + `pyfly.cqrs` + `pyfly.transactional` |

---

## Phase 3 — Enterprise Integrations ✅ **Complete (v26.05.01)**

| Module | Description | Java Source | Status |
|--------|-------------|-------------|--------|
| **Notifications** | Email, SMS, and push notifications with provider adapters (SendGrid, Resend, SMTP, Twilio, Firebase, dummy) | [`fireflyframework-notifications`](https://github.com/fireflyframework/fireflyframework-notifications) | Done in v26.05.01 |
| **IDP** | Identity-provider port + Keycloak / AWS Cognito / Azure AD / internal-DB adapters | [`fireflyframework-idp`](https://github.com/fireflyframework/fireflyframework-idp) | Done in v26.05.01 |
| **ECM** | Enterprise Content Management — documents, folders, e-signature (S3 / Azure Blob / local-fs storage; DocuSign / Adobe Sign / Logalty / no-op signing) | [`fireflyframework-ecm`](https://github.com/fireflyframework/fireflyframework-ecm) | Done in v26.05.01 |
| **Webhooks** | Inbound webhook ingestion with HMAC validation, idempotency, listener dispatch | [`fireflyframework-webhooks`](https://github.com/fireflyframework/fireflyframework-webhooks) | Done in v26.05.01 |
| **Callbacks** | Outbound callback dispatcher with HMAC signing, retries, authorized domains, execution tracking | [`fireflyframework-callbacks`](https://github.com/fireflyframework/fireflyframework-callbacks) | Done in v26.05.01 |
| **Config Server** | Centralized configuration server (`ConfigServer` / `ConfigClient`) with filesystem, in-memory, and Git backends; tiered search-locations overlay | [`fireflyframework-config-server`](https://github.com/fireflyframework/fireflyframework-config-server) | Done — Git backend added in v26.06.92 |

---

## Phase 4 — Administrative & DDD 🔄 **Partially complete (DDD done in v26.05.02)**

| Module | Description | Java Source | Status |
|--------|-------------|-------------|--------|
| **Domain (DDD starters)** | `Entity[TID]`, `ValueObject`, `AggregateRoot[TID]`, `DomainEvent`, `Specification`, `DomainRepository`, `BusinessRuleViolation`, `AggregateNotFound`, plus the `enable_domain_stack` decorator. Pure-Python primitives with zero runtime dependencies. Includes a complete DDD sample under `samples/lumen/` (the book's Lumen wallet & ledger service). | [`fireflyframework-starter-domain`](https://github.com/fireflyframework/fireflyframework-starter-domain) | Done in v26.05.02 |
| **Backoffice** | Admin/backoffice layer with impersonation and enhanced audit | [`fireflyframework-backoffice`](https://github.com/fireflyframework/fireflyframework-backoffice) | Planned |
| **Utils** | Shared utility library — template rendering, filtering, common helpers | [`fireflyframework-utils`](https://github.com/fireflyframework/fireflyframework-utils) | Planned |

---

## Firefly Framework Ecosystem

PyFly is part of the broader [Firefly Framework](https://github.com/fireflyframework) ecosystem:

| Platform | Repository | Status |
|----------|-----------|--------|
| **Java / Spring Boot** | [`fireflyframework-*`](https://github.com/fireflyframework) (40+ modules) | Production |
| **.NET 9** | [`fireflyframework-dotnet`](https://github.com/fireflyframework/fireflyframework-dotnet) | Beta (CalVer 26.05+) |
| **Python** | [`fireflyframework-pyfly`](https://github.com/fireflyframework/fireflyframework-pyfly) | Beta (CalVer 26.05+) |
| **Frontend (Angular)** | [`flyfront`](https://github.com/fireflyframework/flyfront) | Active Development |
| **GenAI** | [`fireflyframework-genai`](https://github.com/fireflyframework/fireflyframework-genai) | Active Development |
| **CLI (Go)** | [`fireflyframework-cli`](https://github.com/fireflyframework/fireflyframework-cli) | Active Development |
