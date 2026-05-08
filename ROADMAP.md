# Roadmap

PyFly's roadmap is driven by achieving feature parity with the full [Firefly Framework](https://github.com/fireflyframework) Java ecosystem (40+ Spring Boot modules). This document tracks which modules are planned and their priority.

---

## Current State (v26.05.03)

PyFly ships with **39 fully-implemented modules** covering the foundation, application, infrastructure, integration, and cross-cutting layers — including the rewritten transactional engine (Saga + Workflow + TCC), Event Sourcing, IDP, ECM, Notifications, Webhooks, Callbacks, Plugins, Rule Engine, Config Server, and the **`pyfly.domain` DDD primitives** (`v26.05.02`). The starter system reached Java/.NET parity in `v26.05.03`: declarative `@enable_*_stack` decorators now actually activate the bundle's property defaults at boot, an imperative `register_*_stack(app)` API mirrors .NET's `services.AddFireflyXxx(...)`, and a new `@enable_web_stack` ships dedicated web-tier wiring. See the [Changelog](CHANGELOG.md) for full details.

Phases 1, 2, and 3 of the original roadmap landed in `v26.05.01`. The **DDD starters** portion of Phase 4 landed in `v26.05.02`, and the **layered-bundle starter system** reached parity with the Java and .NET ports in `v26.05.03`. Backoffice and Utils remain planned.

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
| **Rule Engine** | YAML DSL-based business rule engine with AST evaluation, audit trails, batch evaluation, hot reload | [`fireflyframework-rule-engine`](https://github.com/fireflyframework/fireflyframework-rule-engine) | Done in v26.05.01 |
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
| **Config Server** | Centralized configuration server (`ConfigServer` / `ConfigClient`) with filesystem + in-memory backends | [`fireflyframework-config-server`](https://github.com/fireflyframework/fireflyframework-config-server) | Done in v26.05.01 |

---

## Phase 4 — Administrative & DDD 🔄 **Partially complete (DDD done in v26.05.02)**

| Module | Description | Java Source | Status |
|--------|-------------|-------------|--------|
| **Domain (DDD starters)** | `Entity[TID]`, `ValueObject`, `AggregateRoot[TID]`, `DomainEvent`, `Specification`, `DomainRepository`, `BusinessRuleViolation`, `AggregateNotFound`, plus the `enable_domain_stack` decorator. Pure-Python primitives with zero runtime dependencies. Includes a complete OrderService sample under `samples/order_service/`. | [`fireflyframework-starter-domain`](https://github.com/fireflyframework/fireflyframework-starter-domain) | Done in v26.05.02 |
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
