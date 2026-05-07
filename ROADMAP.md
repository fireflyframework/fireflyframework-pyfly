# Roadmap

PyFly's roadmap is driven by achieving feature parity with the full [Firefly Framework](https://github.com/fireflyframework) Java ecosystem (40+ Spring Boot modules). This document tracks which modules are planned and their priority.

---

## Current State (v0.2.0-M11)

PyFly ships with **27 modules** covering the foundation, application, infrastructure, and cross-cutting layers. See the [Changelog](CHANGELOG.md) for full details on what's included.

---

## Phase 1 — Core Distributed Patterns

| Module | Description | Java Source |
|--------|-------------|-------------|
| **Event Sourcing** | Event store, aggregate roots, snapshots, projections, outbox pattern | [`fireflyframework-eventsourcing`](https://github.com/fireflyframework/fireflyframework-eventsourcing) |
| ~~**Saga / Transactions**~~ | ~~Distributed Saga and TCC transaction orchestration with compensation and recovery~~ | ~~[`fireflyframework-transactional-engine`](https://github.com/fireflyframework/fireflyframework-transactional-engine)~~ **Done in v0.1.0-M5** |
| **Workflow** | Workflow orchestration engine with state persistence, scheduling, and DLQ | [`fireflyframework-workflow`](https://github.com/fireflyframework/fireflyframework-workflow) |
| **Domain** | DDD building blocks — base entities, value objects, aggregate roots, domain events | [`fireflyframework-starter-domain`](https://github.com/fireflyframework/fireflyframework-starter-domain) |

---

## Phase 2 — Business Logic

| Module | Description | Java Source |
|--------|-------------|-------------|
| **Rule Engine** | YAML DSL-based business rule engine with AST evaluation and audit trails | [`fireflyframework-rule-engine`](https://github.com/fireflyframework/fireflyframework-rule-engine) |
| **Plugins** | Plugin system with annotation-based discovery, lifecycle, and dependency resolution | [`fireflyframework-plugins`](https://github.com/fireflyframework/fireflyframework-plugins) |
| **Data Processing** | Job orchestration, enrichment pipelines, CQRS integration for batch workloads | [`fireflyframework-data`](https://github.com/fireflyframework/fireflyframework-data) |

---

## Phase 3 — Enterprise Integrations

| Module | Description | Java Source |
|--------|-------------|-------------|
| **Notifications** | Email, SMS, and push notification abstractions with provider adapters (SendGrid, Twilio, Firebase) | [`fireflyframework-notifications`](https://github.com/fireflyframework/fireflyframework-notifications) |
| **Identity Provider** | IDP abstraction with adapters for Keycloak, AWS Cognito, and internal DB | [`fireflyframework-idp`](https://github.com/fireflyframework/fireflyframework-idp) |
| **ECM** | Enterprise Content Management — document storage, versioning, e-signature (Adobe Sign, DocuSign) | [`fireflyframework-ecm`](https://github.com/fireflyframework/fireflyframework-ecm) |
| **Webhooks** | Inbound webhook ingestion with signature validation, rate limiting, and idempotency | [`fireflyframework-webhooks`](https://github.com/fireflyframework/fireflyframework-webhooks) |
| **Callbacks** | Outbound event dispatching to external systems with circuit breakers and retry | [`fireflyframework-callbacks`](https://github.com/fireflyframework/fireflyframework-callbacks) |

---

## Phase 4 — Administrative & Infrastructure

| Module | Description | Java Source |
|--------|-------------|-------------|
| **Backoffice** | Admin/backoffice layer with impersonation and enhanced audit | [`fireflyframework-backoffice`](https://github.com/fireflyframework/fireflyframework-backoffice) |
| **Config Server** | Centralized configuration server for multi-service deployments | [`fireflyframework-config-server`](https://github.com/fireflyframework/fireflyframework-config-server) |
| **Utils** | Shared utility library — template rendering, filtering, common helpers | [`fireflyframework-utils`](https://github.com/fireflyframework/fireflyframework-utils) |

---

## Firefly Framework Ecosystem

PyFly is part of the broader [Firefly Framework](https://github.com/fireflyframework) ecosystem:

| Platform | Repository | Status |
|----------|-----------|--------|
| **Java / Spring Boot** | [`fireflyframework-*`](https://github.com/fireflyframework) (40+ modules) | Production |
| **Python** | [`pyfly`](https://github.com/fireflyframework/fireflyframework-pyfly) | Milestone (M6) |
| **Frontend (Angular)** | [`flyfront`](https://github.com/fireflyframework/flyfront) | Active Development |
| **GenAI** | [`fireflyframework-genai`](https://github.com/fireflyframework/fireflyframework-genai) | Active Development |
| **CLI (Go)** | [`fireflyframework-cli`](https://github.com/fireflyframework/fireflyframework-cli) | Active Development |
