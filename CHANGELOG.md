# Changelog

All notable changes to PyFly will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## v26.05.01 (2026-05-07)

### CalVer migration

PyFly switches from SemVer-with-milestone (`0.X.Y-MN`) to **Calendar
Versioning** (`YY.MM.PATCH`), aligning with every other Firefly Framework
sibling (Java, .NET, Go) on the same monthly release cadence. See
[`docs/versioning.md`](docs/versioning.md) for the full convention.

The `26.05.01` release ships exactly the same code as `0.3.0-M1`. No
behaviour change — only:

- `pyproject.toml` `version` field: `0.3.0a1` → `26.5.1` (PEP 440 normalised
  form of `26.05.01`).
- `pyfly.__version__`: `"0.2.0-M11"` → `"26.05.01"`.
- `install.sh` `PYFLY_VERSION`: `0.2.0-M11` → `26.05.01`.
- README badge, install commands, "Current" line, and CLI doctor example all
  show the new version.
- `docs/versioning.md` rewritten for the CalVer convention.
- `Development Status` classifier bumped to `4 - Beta` to reflect the
  Java-parity payload that landed under the previous milestone.

The previous tag (`v0.3.0-M1`) and its GitHub release stay in place for
historical reference; new clones, badges, and install-from-release links
should use `v26.05.01`.

---

## v0.3.0-M1 (2026-05-07)

### Java framework parity push — major release

This milestone closes the parity gap with the Java Firefly Framework
(``fireflyframework-orchestration`` and the surrounding modules). It rewrites
the transactional engine from scratch and adds nine missing modules.

### Added — additional adapters & meta-packages
- **IDP adapters**: ``KeycloakIdpAdapter``, ``AwsCognitoIdpAdapter``,
  ``AzureAdIdpAdapter`` (alongside the existing ``InternalDbIdpAdapter``).
- **ECM storage adapters**: ``AwsS3StorageAdapter``, ``AzureBlobStorageAdapter``.
- **ECM e-signature adapters**: ``DocuSignESignatureAdapter``,
  ``AdobeSignESignatureAdapter``, ``LogaltyESignatureAdapter``.
- **Notification provider adapters**: ``SendGridEmailProvider``,
  ``TwilioSmsProvider``, ``FirebasePushProvider``, ``ResendEmailProvider``.
- **Client protocols**: ``SoapClient``/``SoapClientBuilder``,
  ``GrpcClientBuilder``, ``GraphQLClient``/``GraphQLClientBuilder``,
  ``WebSocketClient``/``WebSocketClientBuilder``.
- **Config server**: ``pyfly.config_server`` with
  ``ConfigServer``/``ConfigClient`` and filesystem + in-memory backends.
- **Starter meta-packages**: ``pyfly.starters`` exposing
  ``enable_core_stack``, ``enable_application_stack``, ``enable_data_stack``,
  ``enable_domain_stack`` mirroring the Java starter modules.
- **Extra domain validators**: ``is_valid_date``, ``is_valid_datetime``,
  ``is_valid_national_id``, ``is_valid_sort_code``, ``is_valid_interest_rate``.

### Added — transactional engine, complete rewrite
- **`pyfly.transactional.core`** — new shared foundation: `ExecutionContext`,
  `ExecutionStatus`, `ExecutionPattern`, `RetryPolicy`, `TopologyBuilder`,
  `ArgumentResolver`, `StepInvoker`, `BackpressureStrategy` (adaptive,
  batched, circuit-breaker), `OrchestrationEvents` /
  `CompositeOrchestrationEvents` / `LoggerOrchestrationEvents`,
  `OrchestrationMetrics`, `OrchestrationTracer`, `DeadLetterService`,
  `RecoveryService`, `OrchestrationScheduler`, `OrchestrationValidator`,
  `EventGateway`, `ExecutionReport`, `InMemoryPersistenceProvider`.
- **`pyfly.transactional.workflow`** — entirely new pattern: `@workflow`,
  `@workflow_step`, `@wait_for_signal`, `@wait_for_timer`,
  `@wait_for_all`/`@wait_for_any`, `@child_workflow`, `@compensation_step`,
  `@workflow_query`, `@on_workflow_complete`/`@on_workflow_error`,
  `@scheduled_workflow`, plus `WorkflowEngine`, `WorkflowExecutor`,
  `WorkflowRegistry`, `SignalService`, `TimerService`,
  `ChildWorkflowService`, `ContinueAsNewService`, `WorkflowQueryService`,
  `WorkflowBuilder`.
- **Persistence adapters** — `pyfly.transactional.persistence.RedisPersistenceProvider`,
  `CachePersistenceProvider`, `SqlAlchemyPersistenceProvider`.
- **REST controllers** — `OrchestrationController`, `DeadLetterController`,
  `WorkflowController` exposing list/start/signal/retry endpoints.
- **HealthIndicator** + composite `OrchestrationHealthIndicator`.
- `OrchestrationBuilder` root + `SagaBuilder` + `TccBuilder` for programmatic
  pattern definition.
- `@scheduled_saga`, `@scheduled_tcc`, `@step_event`, `@tcc_event`
  annotations.

### Added — new modules
- **`pyfly.eventsourcing`** — `AggregateRoot`, `EventStore` (in-memory and
  SQLAlchemy), `SnapshotStore`, `TransactionalOutbox`, `Projection` /
  `ProjectionRunner`, `EventUpcaster`, `EventSourcedRepository`.
- **`pyfly.callbacks`** — outbound callback dispatcher with HMAC signing,
  retries, configurable subscriptions and execution tracking.
- **`pyfly.webhooks`** — inbound webhook ingestion with signature validation,
  idempotency dedup, and pluggable listeners.
- **`pyfly.notifications`** — email / SMS / push abstractions with port
  pattern and dummy + SMTP adapters.
- **`pyfly.idp`** — identity provider port + internal-DB adapter with bcrypt
  password hashing, user / session / role management.
- **`pyfly.ecm`** — document storage, metadata, folders, e-signature ports
  with local-filesystem and no-op adapters.
- **`pyfly.plugins`** — pluggable module system: `@plugin`, `@extension`,
  `@extension_point`, `PluginManager`, `PluginDependencyResolver`,
  `ExtensionRegistry`.
- **`pyfly.rule_engine`** — YAML-based business rules with logical
  composition, batch evaluation and an in-memory rule-set repository.

### Added — EDA enhancements
- `EventCircuitBreaker`, `InMemoryEdaDeadLetterStore`,
  `JsonEventSerializer` / `AvroEventSerializer` / `ProtobufEventSerializer`,
  `HeaderEventFilter` / `PredicateEventFilter`.

### Added — domain validators (`pyfly.validation.domain`)
- `is_valid_iban` / `valid_iban`, `is_valid_bic` / `valid_bic`,
  `is_valid_phone_number`, `is_valid_credit_card`, `is_valid_cvv`,
  `is_valid_currency_code`, `is_valid_amount`, `is_valid_account_number`,
  `is_valid_tax_id`, `is_valid_pin`, `is_strong_password`.

### Tests
- 2700+ tests passing, ~135 new tests for the new modules.

---

## v0.2.0-M11 (2026-03-01)

### Fixed
- **Thread-safe singleton initialization**: DI container now uses RLock with double-check pattern to prevent duplicate singleton creation under concurrent access
- **Condition list inheritance**: `@conditional_on_*` decorators now copy conditions via `cls.__dict__` instead of `getattr()` to prevent cross-class mutation through MRO
- **`@transactional` rollback_for semantics**: Replaced `session.begin()` context manager with explicit `begin()`/`commit()`/`rollback()` to support selective rollback matching Spring's `@Transactional`
- **SecurityException status code**: Base `SecurityException` now maps to 403 (Forbidden) instead of 401; `UnauthorizedException` subclass retains 401
- **`@secure` decorator**: Authorization failures now raise `ForbiddenException` (403) instead of base `SecurityException`
- **Security context bridge**: `SecurityMiddleware` now sets `security_context` on both `request.state` and `RequestContext` for `@pre_authorize`/`@post_authorize`
- **Lazy controller race condition**: Added `asyncio.Lock` with double-check to prevent duplicate bean resolution on concurrent first requests
- **Parameter coercion errors**: `_coerce()` now raises `InvalidRequestException` (HTTP 400) instead of unhandled `ValueError`/`TypeError`
- **Bulkhead TOCTOU**: Replaced `semaphore.locked()` check with `_active >= _max_concurrent` for consistent capacity tracking
- **`asyncio.get_event_loop()`**: Replaced 3 occurrences with `get_running_loop()` to avoid deprecation warnings and ensure correct loop in nested contexts

### Changed
- **Resilience sync/async support**: All 4 resilience decorators (`@fallback`, `@rate_limiter`, `@time_limiter`, `@bulkhead`) now detect sync functions via `inspect.iscoroutinefunction` and wrap accordingly
- **Event bus optimization**: Listeners are pre-sorted at subscribe time instead of on every `publish()` call
- **Repository dynamic PK**: `find_all_by_ids()` and `delete_all()` use `_pk_column` property (via `sa_inspect`) instead of hardcoded `.id`
- **Nested repository patching**: `_patch_repositories()` now patches repositories one level deep into nested services
- **Kahn's algorithm**: `_sort_bean_methods` uses `collections.deque` instead of `list.pop(0)` for O(1) popleft
- **Auto-config logging**: `ImportError` during entry point discovery now logged at DEBUG level instead of silently swallowed
- **Exception handling**: `_inject_autowired_fields` catches `NameError` specifically (not bare `Exception`) and logs a warning
- **Filter chain**: Fast path bypasses response buffering when no filters are registered; 100MB body size guard prevents OOM

---

## v0.2.0-M10 (2026-02-28)

### Changed
- **uv-first tooling**: Migrated project from pip-centric to uv-first across all surfaces (CI, scaffolding, installer, docs, error messages)
- **PEP 735 dependency-groups**: Moved dev/test dependencies (`pytest`, `ruff`, `mypy`, `mongomock-motor`, `jsonpath-ng`) from `[project.optional-dependencies].dev` to `[dependency-groups].dev`; `mongomock-motor` now persists across `uv sync`
- **CI workflow**: Changed `uv sync --extra dev` → `uv sync --all-extras --group dev` in all CI jobs
- **Scaffolded templates**: `pyproject.toml.j2` uses `[dependency-groups]`, `readme.md.j2` shows uv-first Quick Start, `dockerfile.j2` uses multi-stage uv build
- **`install.sh`**: Added uv detection — uses `uv pip install` when available, falls back to pip
- **`pyfly doctor`**: Now checks for `uv` instead of `pip` as required package manager
- **Tool-neutral error messages**: All `pip install pyfly[xxx]` messages in source code changed to "Install the xxx extra: pyfly[xxx]"
- **Documentation**: All `docs/**/*.md` and `README.md` updated to show uv as primary tool with pip as fallback

### Removed
- **`cqrs` extra**: Removed empty `[project.optional-dependencies].cqrs = []` (CQRS module has no external deps)
- **`testing` extra**: Moved `jsonpath-ng` to `[dependency-groups].dev`
- **`dev` extra**: Replaced by `[dependency-groups].dev` (PEP 735)

### Added
- **`.python-version`** file (`3.12`) for uv auto-detection

---

## v0.2.0-M9 (2026-02-20)

### Added
- **Method-level security**: `@pre_authorize` and `@post_authorize` decorators with SpEL-style expressions (`hasRole`, `hasPermission`, `isAuthenticated`) evaluated against `RequestContext`
- **DI-aware `@transactional`**: Full transaction management with `Propagation` enum (REQUIRED, REQUIRES_NEW, SUPPORTS, NOT_SUPPORTED, NEVER, MANDATORY) and `Isolation` enum, ContextVar-based session propagation, automatic Repository patching
- **`async_sessionmaker` bean**: Exposed as injectable DI bean for `@transactional` and custom session management
- **Kubernetes probes**: `ProbeGroup` enum (LIVENESS, READINESS), `/actuator/health/liveness` and `/actuator/health/readiness` endpoints with independent indicator grouping
- **Pydantic `@config_properties`**: `Config.bind()` extended to support Pydantic `BaseModel` subclasses with `model_validate()` for fail-fast validation, type coercion, and nested model binding
- **`SoftDeleteMixin`**: Opt-in `deleted_at` column with `is_deleted` property for soft delete support
- **`VersionedMixin`**: Opt-in `version` column with SQLAlchemy `version_id_col` for automatic optimistic locking (`StaleDataError` on concurrent modification)
- **`SoftDeleteRepository`**: Repository subclass with soft-delete-aware CRUD operations (`delete`, `find_all`, `restore`, `hard_delete`, `find_all_including_deleted`)

### Changed
- `RelationalAutoConfiguration`: Refactored to expose `async_session_factory` as a separate bean; `async_session` now depends on it
- `HealthAggregator.add_indicator()`: Accepts optional `groups` parameter for probe group membership

---

## v0.2.0-M8 (2026-02-20)

### Added

- **HttpSecurity DSL**: Fluent builder for URL-level access control — `http_security.authorize_requests().request_matchers("/api/**").authenticated()` with `AccessRule` types (PERMIT_ALL, DENY_ALL, AUTHENTICATED, HAS_ROLE, HAS_ANY_ROLE, HAS_PERMISSION) and RFC 7807 problem+json error responses
- **HttpSecurityFilter**: New WebFilter at HP+350 that evaluates `SecurityRule` chains, reads `SecurityContext` from `request.state`, and returns 401/403 on access denial
- **OAuth2 authorization_code login flow**: `OAuth2LoginHandler` with 3 routes — authorization redirect, callback/code exchange, and session logout — with CSRF state validation and automatic `SecurityContext` persistence in session
- **OAuth2SessionSecurityFilter**: WebFilter at HP+225 that restores `SecurityContext` from session on each request
- **OAuth2LoginAutoConfiguration**: Auto-configures `OAuth2LoginHandler` and `OAuth2SessionSecurityFilter` when `pyfly.security.oauth2.login.enabled=true`
- **Data auditing**: `AuditingEntityListener` wires SQLAlchemy ORM `before_insert`/`before_update` events on `BaseEntity` (with `propagate=True`) to auto-populate `created_at`, `updated_at`, `created_by`, and `updated_by` fields from `RequestContext`
- **`@query` for MongoDB**: `MongoQueryExecutor` compiles `@query`-decorated repository methods into Beanie `find()` or `aggregate()` operations with `:param` placeholder substitution
- **Shared `@query` decorator**: Extracted backend-neutral `@query` decorator to `pyfly.data.query` (re-exported by both SQLAlchemy and MongoDB modules)
- **`@sse_mapping` decorator**: Controller-driven Server-Sent Events — `@sse_mapping("/prices")` on async generator methods, auto-discovered by `SSERegistrar`
- **`SseEmitter`**: High-level SSE emitter with `send()` / `close()` API and async iteration support
- **`format_sse_event()`**: SSE event formatter supporting Pydantic models, dicts, lists, and raw strings with `event:`, `id:`, `retry:` fields

### Changed

- **SecurityFilter ordering**: Moved from default 0 to `HIGHEST_PRECEDENCE + 220` to ensure proper filter chain ordering
- **SecurityContext in RequestContext**: Security filters now write `SecurityContext` into `RequestContext` ContextVar (in addition to `request.state`) for access by data auditing listeners
- **Hexagonal architecture test**: Added `/security/` exemption for httpx imports (required for OAuth2 token exchange)

---

## v0.2.0-M7 (2026-02-19)

### Added

- **WebSocket support**: New `pyfly.websocket` module with `@websocket_mapping` decorator, `WebSocketSession` wrapper, and auto-discovery in `create_app()` via `WebSocketRegistrar`
- **OAuth2 auto-configuration**: Three new auto-configuration classes — `OAuth2ResourceServerAutoConfiguration` (JWKS-based Bearer validation), `OAuth2AuthorizationServerAutoConfiguration` (token endpoint with client_credentials/refresh_token grants), `OAuth2ClientAutoConfiguration` (client registrations from config)
- **OAuth2ResourceServerFilter**: WebFilter for Bearer token validation using `JWKSTokenValidator`, with configurable exclude patterns
- **Session management**: New `pyfly.session` module with `HttpSession`, `SessionStore` Protocol, `InMemorySessionStore`, `RedisSessionStore`, `SessionFilter` middleware, and auto-configuration
- **i18n / locale support**: New `pyfly.i18n` module with `MessageSource` Protocol, `ResourceBundleMessageSource` (YAML/JSON), `AcceptHeaderLocaleResolver`, `FixedLocaleResolver`, and auto-configuration
- **XML serialization**: `dict_to_xml()` / `xml_to_dict()` converters (stdlib xml.etree), `XMLResponse` class, and content negotiation via `Accept` header in controller dispatch
- **`@controller_advice`**: New stereotype for global exception handling across all controllers, with MRO-sorted handler resolution
- **`@shell_method_availability`**: Decorator for conditional shell command registration — unavailable commands are skipped during wiring
- **`@Value` injection**: Existing `Value` descriptor now wired into `Container._inject_autowired_fields()` for config property injection
- **`EventFailureStrategy`**: Configurable strategy (LOG/RAISE) for `CommandBus` event publishing failures

### Fixed

- **InMemoryMessageBroker race condition**: Added `asyncio.Lock` to protect subscriptions and group iterators from concurrent access
- **InMemoryPersistenceAdapter race condition**: Added `asyncio.Lock` to protect saga state store from concurrent mutations
- **Background task lifecycle**: Background tasks created during startup are now tracked and cancelled on `stop()`
- **ASGI pathsend OOM risk**: Changed `read_bytes()` to chunked 64 KB streaming for large file responses

---

## v0.2.0-M6 (2026-02-19)

### Fixed

- **ASGI pathsend extension**: `WebFilterChainMiddleware` now handles the ASGI `http.response.pathsend` extension correctly when running under Granian

---

## v0.2.0-M5 (2026-02-19)

### Added

- **Auto-configuration audit**: 8 new auto-configuration classes bring total to 20, completing Spring Boot-style auto-wiring for all applicable modules
  - `JwtAutoConfiguration` — auto-wires `JWTService` when `pyjwt` is installed and `pyfly.security.enabled=true`
  - `PasswordEncoderAutoConfiguration` — auto-wires `BcryptPasswordEncoder` when `bcrypt` is installed and `pyfly.security.enabled=true`
  - `SchedulingAutoConfiguration` — auto-wires `TaskScheduler` when `croniter` is installed
  - `MetricsAutoConfiguration` — auto-wires `MetricsRegistry` when `prometheus_client` is installed
  - `TracingAutoConfiguration` — auto-wires `TracerProvider` when `opentelemetry` is installed
  - `ActuatorAutoConfiguration` — auto-wires `ActuatorRegistry` and `HealthAggregator` when `pyfly.web.actuator.enabled=true`
  - `MetricsActuatorAutoConfiguration` — auto-wires `MetricsEndpoint` and `PrometheusEndpoint` when actuator is enabled and `prometheus_client` is installed
  - `AopAutoConfiguration` — auto-wires `AspectBeanPostProcessor` unconditionally (always active)
- **Stdlib logging fallback**: `StdlibLoggingAdapter` provides zero-dependency logging when `structlog` is not installed, eliminating the hard `structlog` import in `application.py`
- **Post-processor deduplication**: `ApplicationContext._discover_post_processors()` now performs type-level deduplication, preventing double-weaving when both manual and auto-config post processors exist
- **Container-managed scheduler**: `_wire_scheduled()` now prefers a container-managed `TaskScheduler` bean from auto-configuration before creating one ad-hoc

### Changed

- **MongoDB guarded imports**: `pyfly.data.document.mongodb.__init__` wraps all beanie/motor imports in `try/except ImportError`, preventing crashes when optional dependencies aren't installed

### Fixed

- **Hard import crash**: `pyfly.core.application` no longer crashes with `ImportError` when `structlog` is not installed

## v0.2.0-M4 (2026-02-18)

### Added

- **Pure ASGI middleware**: Rewrote `WebFilterChainMiddleware`, `SecurityMiddleware`, `SecurityHeadersMiddleware`, and `RequestLoggingMiddleware` as pure ASGI middleware classes, eliminating the `BaseHTTPMiddleware` dependency. Fixes `ModuleNotFoundError: No module named 'anyio._backends'` when running with Granian
- **Built-in process metrics**: `MetricsProvider` now collects CPU time (user/system), memory RSS, PID, thread count, uptime, open file descriptors, GC stats per generation, and Python version/implementation without requiring `prometheus_client`. Prometheus metrics are included when available
- **Bean category inference**: Beans without an explicit `@stereotype` decorator are classified by class name suffix (AutoConfiguration, Adapter, Provider, Filter, Middleware, Handler, Factory, Listener) or as "component" instead of "none"
- **Mapping detail panel**: Mappings view shows handler parameters (with types and path/query/body kind), return type, docstring, and response model via `inspect.signature()` extraction. Method breakdown stat cards added
- **Logger descriptions**: 25+ known logger prefixes mapped to human-readable descriptions (e.g., `pyfly.web` → "Web layer (HTTP, routing, filters)"). Reset button returns loggers to NOTSET. Level changes re-fetch to verify
- **Trace detail panel**: Traces now capture query string, client host, content type, user agent, and response content-length. Click-to-detail panel and status code filter pills (All, 2xx, 3xx, 4xx, 5xx) added
- **Wallboard subtitles**: Each tile shows a contextual subtitle (e.g., health component count, top bean stereotype, heap percentage, server version)

### Changed

- **Admin sidebar**: Logo increased from 26px to 36px, vertical divider added, text changed from "Admin" to "Admin Dashboard"
- **Wallboard health tile**: Color is now dynamic based on status (UP → green, DOWN → red, DEGRADED → warning, UNKNOWN → muted) instead of always green
- **Wallboard beans tile**: Shows just the total count instead of verbose stereotype breakdown
- **Wallboard requests tile**: Now displays live trace count from the trace collector instead of "--"
- **Configuration view**: Exposes all top-level config namespaces (e.g., `myapp.*`, `redis.*`), not just `pyfly.*`
- **Metrics view**: Shows built-in/Prometheus source breakdown and metric descriptions

---

## v0.2.0-M3 (2026-02-18)

### Added

- **Admin Wallboard Enhancement**: 9-tile 3x3 grid layout with CPU, GC, Server, and Requests tiles plus live SSE updates
- **Bean Dependency Graph Enhancement**: Click-to-detail panel, real-time search toolbar, stereotype filter pills, dependency highlighting (outgoing/incoming), enhanced tooltips, stats bar
- **Graceful Shutdown**: SQLAlchemy engine lifecycle bean for proper connection pool disposal, SSE stream cancellation handling, configurable shutdown timeout (`pyfly.context.shutdown-timeout`) with `asyncio.wait_for` protection

### Changed

- **Server Startup Output**: Clean Spring Boot-style banner — printed once from CLI process before workers spawn, native server logging suppressed, per-worker startup logs silenced when `workers > 1`
- **Default Workers**: Changed from `os.cpu_count()` to `1` (Spring Boot parity — explicit opt-in for multi-worker)
- **Decorator Typing**: Conditional decorator factories (`conditional_on_class`, `conditional_on_missing_bean`, `conditional_on_property`, `conditional_on_bean`) now return `Callable[[F], F]` instead of `Any`, fixing all `untyped-decorator` mypy strict errors

---

## v0.2.0-M2 (2026-02-18)

### Added

- **Server Abstraction Layer**: Pluggable ASGI servers via `ApplicationServerPort` protocol
  - Granian adapter (Rust/tokio, ~3x faster than Uvicorn, default when installed)
  - Uvicorn adapter (ecosystem standard, fallback)
  - Hypercorn adapter (HTTP/2 and HTTP/3 support)
- **Event Loop Layer**: Pluggable event loops via `EventLoopPort` protocol
  - uvloop (Linux/macOS), winloop (Windows), asyncio (fallback)
- **FastAPI Web Adapter**: First-class peer to Starlette with native OpenAPI support
  - `FastAPIControllerRegistrar` for `@rest_controller` bean discovery
  - Auto-configuration: FastAPI preferred over Starlette when both installed
- **Server Configuration**: `pyfly.server.*` YAML properties with Granian-specific tuning
- **CLI**: `pyfly run --server granian|uvicorn|hypercorn --workers N`
- **Admin Dashboard**: Server info tab with live SSE metrics
- **Scaffolding**: `fastapi-api` archetype in `pyfly new`
- **Installer**: New extras (web-fast, web-fastapi, fastapi, granian, hypercorn)

---

## v0.1.0-M6 (2026-02-18)

### Added

- **`web` archetype** (`pyfly new --archetype web`) — New scaffolding archetype for server-rendered HTML applications. Generates `@controller` endpoints with Jinja2 `TemplateResponse`, `@service` page data providers, HTML templates (`base.html`, `home.html`, `about.html`), static CSS assets, and `StaticFiles` mounting in `main.py`. Includes `jinja2>=3.1` dependency. Default feature: `web`
- **`@controller` runtime support** — `ControllerRegistrar` now discovers both `@rest_controller` and `@controller` stereotypes for route registration. Controllers returning Starlette `Response` objects (e.g., `TemplateResponse`) are passed through unchanged
- **`Request` parameter injection** — `ParameterResolver` now supports injecting the raw Starlette `Request` object into controller method parameters via type hint (`request: Request`). Required for `TemplateResponse` rendering
- **Admin log viewer** — Real-time log viewer with SSE live tail, level-based color-coded badges, filter toolbar (All/ERROR/WARNING/INFO/DEBUG), pause/resume streaming, clear buffer, and auto-scroll. `AdminLogHandler` ring buffer (2000 records) with structlog `ConsoleRenderer` parsing and ANSI escape code stripping
- **Admin cache introspection** — Enhanced cache view with adapter stats (type, entry count), key listing with search, per-key eviction, and bulk evict-all. `InMemoryCache.get_stats()` / `get_keys()` and `RedisCacheAdapter.get_stats()` / `get_keys()` via duck-typed provider

### Changed

- **Web API archetype** — Replaced generic "Item" CRUD example with a "Todo" CRUD example (`title`, `completed`, `description` fields). Files renamed: `item_controller.py` → `todo_controller.py`, `item_service.py` → `todo_service.py`, `item.py` → `todo.py`, `item_repository.py` → `todo_repository.py`, `test_item_controller.py` → `test_todo_service.py`. Added `PUT /todos/{todo_id}` for toggling completion
- **Hexagonal archetype** — All hex templates updated from "Item" to "Todo" naming with `completed` field, `toggle_complete()` methods, and `PUT` mapping in controllers
- **Vendor isolation tests** — Updated to exclude `/cli/templates/` from starlette import leak detection (scaffolding templates legitimately contain starlette imports)

---

## v0.1.0-M5 (2026-02-17)

### Added

- **Transactional engine** (`pyfly.transactional`) — Full port of `fireflyframework-transactional-engine` (Java/Spring Boot) to Python/asyncio. Implements two distributed transaction patterns:
  - **SAGA pattern** — `@saga` and `@saga_step` decorators with DAG-based topological execution, 5 compensation policies (STRICT_SEQUENTIAL, GROUPED_PARALLEL, RETRY_WITH_BACKOFF, CIRCUIT_BREAKER, BEST_EFFORT_PARALLEL), parameter injection via `typing.Annotated` markers (`Input`, `FromStep`, `Header`, `Variable`, `SetVariable`, `FromCompensationResult`, `CompensationError`), retry with exponential backoff and jitter, step timeout via `asyncio.wait_for`, layer concurrency via `asyncio.Semaphore`
  - **TCC pattern** — `@tcc` and `@tcc_participant` decorators with `@try_method`, `@confirm_method`, `@cancel_method` for three-phase Try-Confirm-Cancel transactions, participant ordering, timeout and retry support
  - **Saga composition** — `SagaCompositionBuilder` fluent DSL for orchestrating multiple sagas into a DAG with cross-saga data flow and compensation management
  - **Persistence** — `TransactionalPersistencePort` protocol with `InMemoryPersistenceAdapter` default, state tracking for saga and TCC executions, recovery service for stale/in-flight sagas
  - **Observability** — `TransactionalEventsPort` protocol with `LoggerEventsAdapter` and `CompositeEventsAdapter`
  - **Backpressure** — `BackpressureStrategyPort` protocol with 3 strategies: `AdaptiveBackpressureStrategy`, `BatchedBackpressureStrategy`, `CircuitBreakerBackpressureStrategy`
  - **Compensation error handling** — `CompensationErrorHandlerPort` protocol with 4 handlers: `FailFastHandler`, `LogAndContinueHandler`, `RetryWithBackoffHandler`, `CompositeCompensationErrorHandler`
  - **Auto-configuration** — `TransactionalEngineAutoConfiguration` with 14 `@bean` factory methods, enabled via `pyfly.transactional.enabled=true`
  - **681 tests** including end-to-end integration tests

---

## v0.1.0-M4 (2026-02-17)

### Added

- **Admin dashboard** (`pyfly.admin`) — Spring Boot Admin-inspired embedded management dashboard for monitoring PyFly applications at runtime. 15 built-in views: Overview, Health, Beans, Environment, Configuration, Loggers, Metrics, Scheduled Tasks, HTTP Traces, Mappings, Caches, CQRS (with bus pipeline introspection), Transactions (Saga/TCC visualization), Log Viewer, and Instances (server mode). Real-time SSE updates for health, metrics, and traces (`/admin/api/sse/*`). Server mode with `InstanceRegistry` and `StaticDiscovery` for multi-instance fleet monitoring. Client registration for auto-announcing to a remote admin server. Extensible via `AdminViewExtension` protocol for custom views. Zero-build vanilla JavaScript SPA frontend served from `pyfly.admin.static`. No additional dependencies beyond `pyfly[web]`. Enable with `pyfly.admin.enabled: true`
- **CLI archetype** (`pyfly new --archetype cli`) — New scaffolding archetype for command-line applications. Generates `@shell_component` commands, `@service` business logic, non-ASGI `main.py` entry point, and shell-enabled `pyfly.yaml`. Includes `shell` feature in the feature system with `FEATURE_GROUPS`, `FEATURE_DETAILS`, and `FEATURE_TIPS` entries
- **Shell subsystem** (`pyfly.shell`) — Spring Shell-inspired CLI framework with full DI integration. `@shell_component` stereotype for command classes, `@shell_method` for command declarations, `@shell_option` / `@shell_argument` for explicit parameter overrides. Automatic parameter inference from type hints (positional args, `--options`, `--flags`). `ShellRunnerPort` protocol with `ClickShellAdapter` (Click 8.1+). `CommandLineRunner` and `ApplicationRunner` protocols for post-startup hooks. `ApplicationArguments` for parsed CLI argument access. `ShellAutoConfiguration` via `pyfly.auto_configuration` entry point (enabled with `pyfly.shell.enabled=true`). Install via `pip install pyfly[shell]`

### Changed

- **Decentralized auto-configuration** — `AutoConfigurationEngine` has been removed. Each subsystem now owns its own `@auto_configuration` class (e.g. `CacheAutoConfiguration`, `MessagingAutoConfiguration`, `WebAutoConfiguration`, `RelationalAutoConfiguration`, `DocumentAutoConfiguration`, `ClientAutoConfiguration`). The central `AutoConfigurationEngine.configure()` call is replaced by `discover_auto_configurations()` which discovers `@auto_configuration` classes via the `pyfly.auto_configuration` entry-point group. Third-party packages can register their own auto-configuration classes through the same mechanism. Provider detection (`detect_provider()`) now lives inside each subsystem's auto-configuration class; the core `AutoConfiguration` class only exposes the generic `is_available()` helper
- **`ApplicationContext` Beanie initialization** — `_initialize_beanie()` has been removed from `ApplicationContext`. Beanie ODM initialization is now handled by `BeanieInitializer`, a lifecycle bean registered by `DocumentAutoConfiguration`
- **`SecurityMiddleware` relocated** — Canonical location moved from `pyfly.security` to `pyfly.web.adapters.starlette.security_middleware`. JWT enforcement is now handled by `SecurityFilter` (a `WebFilter` in the filter chain); `SecurityMiddleware` is retained as a `BaseHTTPMiddleware` for backward compatibility
- **`ServiceClient` class removed** — The `pyfly.client.service_client` module (containing the `ServiceClient` class) has been deleted. The `@service_client` decorator remains available in `pyfly.client.declarative` and is exported from `pyfly.client`. HTTP client functionality is provided by the declarative `@http_client` / `@service_client` interface and `HttpClientPort` adapter
- **`pyfly.observability` consolidated** — `pyfly.observability.health` and `pyfly.observability.logging` removed; health and logging concerns are handled by `pyfly.actuator` and `pyfly.logging` respectively
- **`pyfly.cache.types` removed** — Cache type definitions consolidated into `pyfly.cache` package exports

---

## v0.1.0-M3 (2026-02-15)

### Added

- **Spring Data umbrella refactoring** — `pyfly.data` is now a pure commons layer (Page, Pageable, ports, QueryMethodParser). Relational modules moved to `pyfly.data.relational` (Specification, Filter, Query, SQLAlchemy adapter). Document modules moved to `pyfly.data.document` (MongoDB/Beanie adapter). Config prefixes changed to `pyfly.data.relational.*` and `pyfly.data.document.*`. Feature names renamed to `data-relational` and `data-document`. Properties renamed to `RelationalProperties` and `DocumentProperties`
- **MongoDB/Document Database Support** (`pyfly.data.document.mongodb`) — `MongoRepository[T, ID]`, `BaseDocument`, `MongoQueryMethodCompiler`, `MongoRepositoryBeanPostProcessor`, `mongo_transactional`. Install via `pip install pyfly[data-document]`. Beanie ODM initialization is handled by `BeanieInitializer` lifecycle bean (registered by `DocumentAutoConfiguration` in M4)
- `DocumentProperties` configuration (`pyfly.data.document.*` — uri, database, pool sizes)
- Auto-detection of Beanie ODM via `AutoConfiguration.detect_document_provider()` [Superseded in M4: detection now handled by `DocumentAutoConfiguration` registered via `@auto_configuration`]
- CLI scaffolding: `--features data-document` generates Beanie documents, MongoRepository, and MongoDB config. Both `data-relational` and `data-document` can be selected together for multi-backend projects
- Derived query method compilation for MongoDB (reuses shared `QueryMethodParser` + `MongoQueryMethodCompiler`)
- New documentation guide: `docs/guides/data-document.md`
- **Generic repository IDs** — `RepositoryPort[T, ID]` and `Repository[T, ID]` are now dual-generic, accepting any primary key type (UUID, int, str). `CrudRepository[T, ID]` and `PagingRepository[T, ID]` were already dual-generic
- **CLI wizard revamp** — Interactive `pyfly new` wizard now uses 4 numbered steps with archetype comparison table, grouped feature selection with `questionary.Separator`, and feature-aware post-generation tips
- **Feature-aware scaffolding** — Templates generate code based on selected features: `Valid[T]` in controllers (replaces `Body[T]`), `Field()` constraints in models, conditional SQLAlchemy `Repository[ItemEntity, int]` (with `data-relational` feature) vs in-memory store, actuator config, `adapter: auto` in pyfly.yaml

### Changed

- **`RepositoryPort`** — **Breaking:** Now `RepositoryPort[T, ID]` instead of `RepositoryPort[T]`. Existing code using `RepositoryPort[MyEntity]` must change to `RepositoryPort[MyEntity, UUID]` (or appropriate ID type)
- **`Repository`** — **Breaking:** Now `Repository[T, ID]` instead of `Repository[T]`. Existing code using `Repository[MyEntity]` must change to `Repository[MyEntity, UUID]` (or appropriate ID type). `bound=BaseEntity` constraint removed — any SQLAlchemy model works
- **Scaffolded controllers** — Now use `Valid[T]` instead of `Body[T]` for structured 422 error responses
- **Scaffolded models** — Now include `Field(min_length=..., max_length=...)` constraints and conditional `ItemEntity(Base)` when data feature is selected
- **Scaffolded pyfly.yaml** — Now includes `adapter: auto` under `web:` and `actuator: endpoints: enabled: true` for non-library archetypes

### Added

- **`Valid[T]` annotation** (`pyfly.web.params`) — Explicit parameter validation marker for controller handlers. `Valid[T]` standalone implies `Body[T]` + structured 422 errors; `Valid[Body[T]]` and `Valid[QueryParam[T]]` wrap inner binding types. Catches Pydantic `ValidationError` and converts to `ValidationException` with `code="VALIDATION_ERROR"` and `context={"errors": [...]}`
- **Config-driven web adapter selection** — New `pyfly.web.adapter` config key (`auto|starlette`). `AutoConfiguration.detect_web_adapter()` checks if Starlette is importable. `AutoConfigurationEngine._configure_web()` registers `StarletteWebAdapter` as a `WebServerPort` bean [Superseded in M4: `AutoConfigurationEngine` removed; web adapter registration now handled by `WebAutoConfiguration` via `@auto_configuration` and entry-point discovery]
- **`StarletteWebAdapter`** — Class-based `WebServerPort` implementation that delegates to `create_app()`, registered via auto-configuration
- **WebFilter chain architecture** — `WebFilterChainMiddleware` wraps all `WebFilter` instances into a single Starlette middleware. Built-in filters: `TransactionIdFilter`, `RequestLoggingFilter`, `SecurityHeadersFilter`, `SecurityFilter`. User filters auto-discovered from DI context
- **`OncePerRequestFilter`** base class — URL-pattern matching via `url_patterns` and `exclude_patterns` (fnmatch globs)
- **`ActuatorEndpoint` protocol** — Extensible actuator endpoint interface with `endpoint_id`, `enabled`, and `handle()`. Custom endpoints auto-discovered from DI container
- **`ActuatorRegistry`** — Collects and manages actuator endpoints with per-endpoint enable/disable via `pyfly.actuator.endpoints.{id}.enabled` config
- **`/actuator` index endpoint** — HAL-style `_links` response listing all enabled endpoints
- **`LoggersEndpoint`** — `GET /actuator/loggers` lists all loggers; `POST /actuator/loggers` changes log levels at runtime
- **`MetricsEndpoint`** — Stub endpoint at `/actuator/metrics` (disabled by default) for future Prometheus/OpenTelemetry integration
- **New documentation guides** — `web-filters.md` (WebFilter chain reference), `custom-actuator-endpoints.md` (extensible actuator guide)

### Changed

- **`WebProperties`** — Added `adapter: str = "auto"` field for config-driven web adapter selection
- **`ParameterResolver`** — `ResolvedParam` dataclass gains `validate: bool` field; resolver inspects and unwraps `Valid[T]` annotations during parameter inspection and resolution
- **`ControllerRegistrar`** — `_extract_param_metadata()` now unwraps `Valid[T]` for correct OpenAPI spec generation
- **`AutoConfigurationEngine.configure()`** — Now calls `_configure_web()` before other subsystem configurations [Superseded in M4: `AutoConfigurationEngine` removed; replaced by `discover_auto_configurations()` with per-subsystem `@auto_configuration` classes]

---

## v0.1.0-M2 (2026-02-15)

### Added

- **Unified Lifecycle protocol** (`pyfly.kernel.Lifecycle`) — All infrastructure adapters now implement a standard `start()`/`stop()` contract for connection management and resource cleanup
- **Fail-fast startup** — If explicitly configured infrastructure (Redis, Kafka, RabbitMQ, etc.) is unreachable, the application fails immediately with `BeanCreationException` instead of starting in a broken state
- **Multi-source config loading** (`Config.from_sources()`) — Auto-discovers and loads config files from a directory with source tracking via `loaded_sources` property
- **Config source logging** — Startup now logs which configuration sources were loaded (framework defaults, user config, profile overlays)
- **Route and API docs logging** — Startup logs mapped HTTP endpoints and API documentation URLs (Swagger UI, ReDoc, OpenAPI)
- **Interactive CLI wizard** (`pyfly new`) — Arrow-key archetype selection and space-bar feature toggling via questionary, with Rich-styled confirmation summary
- **Graceful Ctrl+C handling** — Interactive wizard exits cleanly without traceback on keyboard interrupt

### Changed

- **Port lifecycle standardization** — All infrastructure ports (`HttpClientPort`, `CacheAdapter`, `TaskExecutorPort`, `EventPublisher`) now use `start()`/`stop()` instead of `close()`/`shutdown()`
- **ServiceClient** — `close()` renamed to `stop()`, added `start()` for lifecycle symmetry
- **TaskScheduler.stop()** — No longer accepts `wait` parameter; always performs graceful shutdown
- **BeanCreationException** — Now inherits from `InfrastructureException` (was `Exception`)
- **Auto-configuration engine** — Tracks created adapters for lifecycle management; validation moved from socket-level checks to adapter `start()` methods [Superseded in M4: centralized engine removed; each subsystem's `@auto_configuration` class manages its own adapter lifecycle]
- **Startup sequence** — Added adapter lifecycle phase: infrastructure adapters are started after auto-configuration and stopped in reverse order during shutdown
- **Scan logging deferred** — Package scan results now appear after the banner, not before
- **Uvicorn noise suppressed** — Redundant startup/shutdown messages from uvicorn are suppressed

### Fixed

- **Config key alignment** — Scaffolding templates now use correct `pyfly.*` config keys matching framework defaults
- **Framework defaults** — Default providers set to `memory` (not `auto`) so apps start without external infrastructure

---

## v0.1.0-M1 (2026-02-14) — Initial Release

The first public release of PyFly — the official native Python implementation of the [Firefly Framework](https://github.com/fireflyframework).

### Foundation Layer

- **`pyfly.kernel`** — Unified exception hierarchy with 25+ domain-specific error types, `ErrorResponse`, `ErrorCategory`, `ErrorSeverity`
- **`pyfly.core`** — Application bootstrap (`PyFlyApplication`, `@pyfly_application`), `Config` with YAML/TOML support, profile overlays, banner rendering
- **`pyfly.container`** — DI container with constructor injection, stereotype decorators (`@service`, `@component`, `@repository`, `@controller`, `@rest_controller`, `@configuration`), scopes (singleton, transient, request), `@bean`, `@primary`, `@order`, `Qualifier`
- **`pyfly.context`** — `ApplicationContext`, lifecycle hooks (`@post_construct`, `@pre_destroy`), `BeanPostProcessor`, conditions (`@conditional_on_property`, `@conditional_on_class`, `@conditional_on_bean`, `@conditional_on_missing_bean`), application events
- **`pyfly.config`** — `AutoConfiguration` utility with provider detection helpers, `discover_auto_configurations()` entry-point discovery [Superseded in M4: `AutoConfigurationEngine` removed; provider detection remains in `AutoConfiguration`, but subsystem registration is now handled by per-subsystem `@auto_configuration` classes discovered via entry points]
- **`pyfly.logging`** — `LoggingPort` and `StructlogAdapter` for structured logging

### Application Layer

- **`pyfly.web`** — HTTP routing (`@get_mapping`, `@post_mapping`, etc.), parameter binding (`Body`, `PathVar`, `QueryParam`, `Header`, `Cookie`), CORS, security headers, exception handling, Starlette/ASGI adapter
- **`pyfly.data`** — `RepositoryPort`, `SessionPort`, derived query methods (`QueryMethodParser`), `Specification` pattern, `Page`/`Pageable`/`Sort`, `Mapper`, SQLAlchemy async adapter. The `@query` decorator lives in `pyfly.data.relational.sqlalchemy.query`
- **`pyfly.cqrs`** — `Command`, `Query`, `CommandHandler`, `QueryHandler`, `Mediator`, logging and metrics middleware
- **`pyfly.validation`** — `@validate_input`, `@validator`, Pydantic model validation

### Infrastructure Layer

- **`pyfly.security`** — `JWTService`, `BcryptPasswordEncoder`, `SecurityContext`, `@secure`, `SecurityMiddleware` [Superseded in M4: `SecurityMiddleware` relocated to `pyfly.web.adapters.starlette.security_middleware` and integrated as a `WebFilter`]
- **`pyfly.messaging`** — `MessageBrokerPort`, `@message_listener`, adapters for Kafka (`aiokafka`), RabbitMQ (`aio-pika`), and in-memory
- **`pyfly.eda`** — `EventPublisher`, `EventEnvelope`, `@event_listener`, `@publish_result`, `InMemoryEventBus`, `ErrorStrategy`
- **`pyfly.cache`** — `CacheAdapter`, `CacheManager`, `@cacheable`, `@cache_evict`, `@cache_put`, Redis and in-memory adapters
- **`pyfly.client`** — `HttpClientPort`, `ServiceClient`, `CircuitBreaker`, `RetryPolicy`, declarative `@http_client`, HTTPX adapter
- **`pyfly.scheduling`** — `TaskExecutorPort`, `@scheduled`, `@async_method`, `CronExpression`, `TaskScheduler`, asyncio and thread pool executors
- **`pyfly.resilience`** — `@rate_limiter`, `@bulkhead`, `@time_limiter`, `@fallback`

### Cross-Cutting Layer

- **`pyfly.aop`** — `@aspect`, `@before`, `@after`, `@around`, `@after_returning`, `@after_throwing`, `AspectBeanPostProcessor`
- **`pyfly.observability`** — `@timed`, `@counted`, `@span`, `MetricsRegistry`
- **`pyfly.actuator`** — Health, beans, environment, and info endpoints via `ActuatorEndpoint` protocol and `ActuatorRegistry`
- **`pyfly.testing`** — `PyFlyTestCase`, `create_test_container`, event assertions
- **`pyfly.cli`** — `pyfly new`, `pyfly run`, `pyfly info`, `pyfly doctor`, `pyfly db` (init, migrate, upgrade, downgrade), `pyfly license`, `pyfly sbom`

### Tooling

- Interactive `install.sh` installer with venv creation, extras selection, and PATH configuration
- Non-interactive mode for CI/CD with environment variable overrides (`PYFLY_HOME`, `PYFLY_EXTRAS`)
