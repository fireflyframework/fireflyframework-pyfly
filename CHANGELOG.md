# Changelog

All notable changes to PyFly will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## v26.06.62 (2026-06-07)

### Added (hardening — benchmarks + Redis integration tests)

- **`benchmarks/`** — a dependency-free micro-benchmark harness (`uv run python benchmarks/run.py`)
  measuring DI container resolution (singleton/transient/with-deps), Pydantic serialization, and
  the PyFly filter-chain request overhead vs bare Starlette. Surfaces regressions and quantifies
  overhead (e.g. cached singleton resolve ~5.3M ops/s; filter chain ~+26% over bare Starlette;
  transient-with-deps reveals per-resolve `get_type_hints` as a future optimization).
- **`tests/integration/`** — Redis integration tests (testcontainers, `@requires_docker`) that
  exercise `RedisDistributedLock` (SET NX PX, owner-token Lua release, TTL expiry) and
  `RedisSessionRegistry` (sorted-set oldest-first) against a **real Redis**, not fakes.

## v26.06.61 (2026-06-07)

### Added (data — run-on-startup database migrations)

- **`MigrationRunner`** + **`MigrationAutoConfiguration`** — when
  `pyfly.data.relational.migrations.enabled=true`, pyfly applies `alembic upgrade head` on
  startup (Spring Boot Flyway-style auto-migrate), reusing the project's existing Alembic
  environment (`alembic.ini` + `alembic/env.py` from `pyfly db init`) and migrating the same
  datasource as the app. The upgrade runs in a worker thread so the generated async `env.py`
  (which calls `asyncio.run`) isn't nested in the running loop; if `alembic.ini` is absent it
  logs a warning and skips rather than failing startup.
- Config: `pyfly.data.relational.migrations.{enabled,config,revision}`.

## v26.06.60 (2026-06-07)

### Fixed (auto-configuration discovery)

- **Session concurrency control now actually auto-wires.** `SessionConcurrencyAutoConfiguration`
  (v26.06.55) was missing its `pyfly.auto_configuration` entry point, so it was never discovered
  at startup and the `SessionConcurrencyController` bean was never created. Added the entry point.

### Added (guardrail)

- A test that AST-scans every `@auto_configuration` class and asserts each is reachable via an
  entry point — so a forgotten entry point fails CI instead of silently disabling a feature.

## v26.06.59 (2026-06-07)

### Added (distributed primitives — Redis adapters, hexagonal)

The clustering features shipped in v26.06.53/55 now have real cross-process backends. All
adapters are hexagonal — the async Redis client is injected; **no adapter imports redis**, and
the only gated lazy ``import redis.asyncio`` lives in the auto-config composition roots:

- **`RedisDistributedLock`** (`pyfly.scheduling.adapters.redis_lock`) — `SET NX PX` acquire +
  owner-token compare-and-delete release (an instance only releases a lock it still owns).
  Selected via `pyfly.scheduling.lock.provider=redis`.
- **`InProcessDistributedLock`** (`pyfly.scheduling`) — real single-process mutual exclusion
  with TTL self-heal (`provider=memory`); `none` (default) stays the no-op `LocalLock`.
- **`RedisSessionRegistry`** (`pyfly.session.adapters.redis_registry`) — a sorted set per
  principal (oldest-first), for cross-process session concurrency control. Selected via
  `pyfly.session.concurrency.registry=redis`.

Verified by an adversarial review against the installed redis-py (signatures + token/ordering
semantics) — `@scheduled(lock=...)` and `maximumSessions` now actually coordinate across a cluster.

## v26.06.58 (2026-06-07)

### Added (config — runtime reload, completing @RefreshScope)

- **`Config.reload_from_sources()`** — re-reads the original config files/profiles (the exact
  merge `from_sources` performed) and atomically swaps in the result, so file/profile changes
  are picked up at runtime. Returns `False` (no-op) for dict-constructed configs. `get()` reads
  a single atomically-rebound dict, so concurrent readers always see a consistent snapshot.
- **`ContextRefresher.refresh()`** now reloads config first, so a refresh (or
  `POST /actuator/refresh`) rebuilds refresh-scoped and `@config_properties` beans against the
  freshly-read config — not just the live environment. This closes the v26.06.52 follow-up.

## v26.06.57 (2026-06-07)

### Added (container — public registration/introspection SPI)

Promotes the previously-internal `Container._registrations` mutation pattern to a supported
public API (no more reaching into registration internals across modules):

- **`register_instance(cls, instance, *, name="")`** — register an already-constructed object
  as a singleton bean (Spring's `registerSingleton`).
- **`contains_type(cls)`**, **`get_registration(cls)`**, **`registered_types()`** — read-only
  introspection.
- **`reset_instance(cls)`** — drop a singleton's cached instance so it rebuilds on next resolve.

`ApplicationContext`, test slices, and `ContextRefresher` now use this SPI instead of
mutating `_registrations` directly.

## v26.06.56 (2026-06-07)

### Docs (parity documentation for v26.06.37–55)

Documented every feature shipped in v26.06.37–55 across 14 docs files (verified — all
referenced imports/config keys resolve against the released source):

- **dependency-injection**: custom-scope SPI, `@RefreshScope`, `Scope.SESSION`,
  `@conditional_on_single_candidate` / `@conditional_on_web_application` / `@conditional_on_resource`
- **security**: full method-security SpEL, role hierarchy, OAuth2 PKCE
- **observability**: OpenTelemetry distributed-trace propagation
- **configuration**: profile boolean expressions (`& | ! ()`)
- **events**: injectable `ApplicationEventPublisher` + arbitrary domain events
- **caching**: `@cacheable` condition / unless
- **resilience**: `@retry` jitter, circuit-breaker failure-rate window & half-open tuning
- **scheduling**: `@scheduled` time zones + distributed lock
- **messaging**: `@message_listener` retry + dead-letter routing
- **data-relational**: multiple named datasources
- **testing**: functional test slices
- **session**: session concurrency control
- **actuator**: `POST /actuator/refresh`
- **spring-comparison**: parity rows for all of the above (and corrected stale resilience API examples)

## v26.06.55 (2026-06-07)

### Added (session — concurrency control)

Spring Security's `maximumSessions` — limit concurrent sessions per authenticated principal:

- **`SessionConcurrencyController`** + **`ConcurrencyControlPolicy`** (`max_sessions`,
  `strategy`) + a **`SessionRegistry`** port with an `InMemorySessionRegistry`
  (`pyfly.session`). On login, an over-cap principal is either **rejected** (`reject-new`)
  or the **oldest session is evicted** (`evict-oldest`, also deleted from the session store);
  logout deregisters.
- Wired into the OAuth2 login flow (the one point where a principal binds to a session) and
  auto-configured (gated, opt-in) via `pyfly.session.concurrency.enabled` /
  `.max-sessions` / `.strategy`. Unlimited by default — behavior unchanged when not enabled.

## v26.06.54 (2026-06-07)

### Added (security — OAuth2 authorization_code PKCE)

The OAuth2 login flow (`OAuth2LoginHandler`) gained **PKCE** (RFC 7636, S256):

- **`ClientRegistration(use_pkce=True)`** — when enabled, the authorization redirect carries a
  `code_challenge` + `code_challenge_method=S256` (the one-time verifier is stashed in the
  session), and the callback sends the `code_verifier` on the token exchange. Recommended for
  public clients; harmless and more secure for confidential clients.
- Default `use_pkce=False` preserves the existing flow byte-for-byte.

## v26.06.53 (2026-06-07)

### Added (scheduling — @scheduled distributed lock)

- **`@scheduled(..., lock=..., lock_ttl=...)`** (ShedLock / Spring `@SchedulerLock` parity) —
  when a job declares a lock, the scheduler acquires it before each run and **skips the tick**
  if held elsewhere, so only one instance per cluster runs the job. `lock=True` auto-derives
  the name `"Class.method"`; a string sets an explicit shared name; `lock_ttl` (a `timedelta`,
  default 60s) bounds the hold so a crashed instance self-heals.
- New **`DistributedLock`** protocol (`try_acquire`/`release`) + default **`LocalLock`**
  (always acquires — single-instance behavior unchanged). Register a `DistributedLock` bean
  (e.g. Redis-backed) and the scheduler auto-wires it for cross-process coordination.
- The lock is always released in `finally` once the body completes.

## v26.06.52 (2026-06-07)

### Added (context — @RefreshScope + ContextRefresher + POST /actuator/refresh)

Spring Cloud's refresh scope, built on the v26.06.50 custom-scope SPI:

- **`@refresh_scope`** (`pyfly.container`) / `scope="refresh"` — a bean is cached like a
  singleton but evicted on refresh, so the next resolution rebuilds it (re-running injection
  and re-reading `@Value`/env placeholders against the live `Config`). The `"refresh"` scope
  and `RefreshScope` handler are built in (always available).
- **`ContextRefresher`** (`pyfly.context`, injectable) — `await refresher.refresh()` evicts all
  refresh-scoped beans, resets `@config_properties` beans so they re-bind from the live config,
  and publishes a **`RefreshScopeRefreshedEvent`**.
- **`POST /actuator/refresh`** — triggers a refresh and returns the refreshed bean keys
  (web-exposed only when opted in via `management.endpoints.web.exposure.include`).

Config *source* reload (re-reading files) remains a follow-up; this release rebinds beans
against the live `Config` (which already re-reads env vars + `${...}` placeholders).

## v26.06.51 (2026-06-07)

### Added (testing — functional test slices)

- **`web_slice(*controllers, ...)`**, **`service_slice(...)`**, **`data_slice(...)`**, and the
  generic **`slice_context(*beans, ...)`** (`pyfly.testing`) — build a minimal, *started*
  ApplicationContext containing only the beans you pass, with collaborators supplied via an
  `overrides={Interface: fake_or_class}` map (Spring's `@WebMvcTest`/`@DataJpaTest` slices).
  `web_slice` additionally wraps the context in a `PyFlyTestClient` via `create_app(context=...)`.
  Each builder returns an async context manager that stops the context on exit, and **fails
  fast**: the slice's beans are resolved at build time, so a missing collaborator raises
  immediately rather than on first request.

## v26.06.50 (2026-06-07)

### Added (container — custom bean-scope SPI)

- **`Container.register_scope(name, handler)`** / **`unregister_scope(name)`** plus the
  **`ScopeHandler`** protocol (`get(name, object_factory)` / `remove(name)`) — Spring's
  `ConfigurableBeanFactory.registerScope` + `Scope` SPI. Declare a bean with a custom scope
  via `register(cls, scope="my-scope")` or `__pyfly_scope__ = "my-scope"`, and the container
  resolves it through the registered handler. Built-in scope names are reserved; resolving an
  unregistered scope raises a clear error.
- Back-compat: `SINGLETON` / `TRANSIENT` / `REQUEST` / `SESSION` dispatch is byte-for-byte
  unchanged (the new branch is a single `isinstance(reg.scope, str)` check after them).
  `Registration.scope` is widened to `Scope | str`; admin/actuator bean views render the
  scope via a `scope_name()` helper.

## v26.06.49 (2026-06-07)

### Added (context — @ConditionalOnSingleCandidate)

- **`@conditional_on_single_candidate(bean_type)`** — register the bean only when exactly one
  candidate assignable to *bean_type* exists, or when several exist but exactly one is
  `@primary` (Spring Boot's `@ConditionalOnSingleCandidate`). Evaluated in pass 2 and purely
  type/registration-based (never resolves or instantiates a bean). Interface-alias
  registrations are deduped so one implementation of an interface counts once, not twice.

## v26.06.48 (2026-06-07)

### Added (data — multiple named datasources)

- **`NamedDataSources`** — configure secondary datasources under
  `pyfly.data.relational.datasources.<name>.url` (+ optional `echo`); inject `NamedDataSources`
  and call `.get("<name>")` for that datasource's `async_sessionmaker` (Spring's multiple
  `DataSource` beans). `.names()` lists them; `await .dispose()` closes their engines. The
  primary datasource keeps its dedicated beans unchanged; the registry is empty when none
  are configured.

## v26.06.47 (2026-06-07)

### Added (messaging — @message_listener retry + dead-letter routing)

`@message_listener` gained resilience options (Spring Kafka `@RetryableTopic` /
`DefaultErrorHandler` parity), applied adapter-agnostically (Kafka, RabbitMQ, in-memory):

- **`retries`** — re-invoke the handler N times on failure, with linear `retry_delay`
  backoff (attempt N waits `retry_delay * N`).
- **`dead_letter_topic`** — a message still failing after `retries` is re-published there
  with `x-original-topic` / `x-exception` headers, instead of propagating.

With no options set, the handler is wired unchanged (zero overhead). The wrapper lives in
`pyfly.messaging.error_handling.wrap_listener` and is applied during listener wiring.

## v26.06.46 (2026-06-07)

### Added (container — SESSION bean scope)

- **`Scope.SESSION`** — register a bean with `scope=Scope.SESSION` to get one instance per
  HTTP session (Spring's session scope). The instance lives as an `HttpSession` attribute, so
  the `SessionFilter` must be active; it is persisted with the session, so it must be
  serializable when a non-memory session store (e.g. Redis) is used.
- The `SessionFilter` now exposes the active `HttpSession` to the container via the
  `RequestContext`, mirroring how `REQUEST`-scoped beans are resolved.

(A general custom-scope SPI remains on the roadmap; this adds the concrete SESSION scope.)

## v26.06.45 (2026-06-07)

### Added (security — role hierarchy)

- **`RoleHierarchy`** (`pyfly.security`) — declare `ADMIN > MANAGER`, `MANAGER > USER` so a
  higher role implies every authority of the lower ones (Spring Security's `RoleHierarchy`).
  `RoleHierarchy.from_string(...)` parses `HIGHER > LOWER` rules; `.expand(roles)` returns the
  transitive closure.
- **`set_role_hierarchy(...)` / `get_role_hierarchy()`** install the process-wide hierarchy
  consulted by `hasRole` / `hasAnyRole` / `hasAuthority` in all method-security expressions
  (`@pre_authorize` / `@post_authorize` / `@secure`). With no hierarchy set, behavior is
  unchanged (no implicit roles).

## v26.06.44 (2026-06-07)

### Added (scheduling — @scheduled time zones)

- **`@scheduled(cron=..., zone="America/New_York")`** — cron expressions are now evaluated
  in the given IANA time zone (Spring's `@Scheduled(zone=...)`); defaults to UTC.
  `CronExpression` gained a `zone` argument and computes fire times in that zone.

## v26.06.43 (2026-06-07)

### Added (resilience — tuning)

Completes the v26.06.36 resilience decorators toward Resilience4j parity:

- **`@retry(jitter=...)`** — a randomization fraction in ``[0, 1]`` applied to each backoff
  wait (``±jitter * wait``), to avoid thundering-herd retries.
- **`CircuitBreaker` failure-rate window** — set `failure_rate_threshold` (+ `window_size`)
  to open on the failure *rate* over the last N calls (Resilience4j COUNT_BASED) instead of
  consecutive failures.
- **`CircuitBreaker(half_open_max_calls=...)`** — admit N trial calls in HALF_OPEN; that
  many successes close the circuit, any failure re-opens it.

Existing consecutive-failure behavior is unchanged when these options are not set.

## v26.06.42 (2026-06-07)

### Added (cache — @cacheable condition / unless)

`@cacheable` / `cache` now accept Spring-style predicates (Pythonic callables):

- **`condition`** — a predicate over the call arguments; when it returns ``False`` caching
  is bypassed entirely (the function runs, nothing is read from or written to the cache).
- **`unless`** — a predicate over the *result*; when it returns ``True`` the result is
  returned but not stored (e.g. skip caching empty/None results).

## v26.06.41 (2026-06-07)

### Added (context — injectable ApplicationEventPublisher + arbitrary domain events)

- **`ApplicationEventPublisher`** is now an injectable singleton bean (Spring's
  `ApplicationEventPublisher`): inject it into any bean and `await publisher.publish(event)`
  to fire events into the context event bus.
- **Arbitrary domain events** — the event bus and `@app_event_listener` no longer require
  events to subclass `ApplicationEvent`; any object can be published, and a listener whose
  parameter type matches (by `isinstance`) receives it (an untyped/`Any` parameter still
  falls back to the catch-all `ApplicationEvent`).

`ApplicationEventPublisher` is exported from `pyfly.context`.

## v26.06.40 (2026-06-07)

### Added (context — more @ConditionalOn* conditions)

- **`@conditional_on_web_application()`** — registers the bean only when a web stack
  (Starlette or FastAPI) is importable (Spring `@ConditionalOnWebApplication`).
- **`@conditional_on_resource(path)`** — registers only when the filesystem resource at
  *path* exists (Spring `@ConditionalOnResource`).

Both exported from `pyfly.context`. (`@ConditionalOnSingleCandidate` remains on the
roadmap — it needs careful bean-counting around interface-alias registrations.)

## v26.06.39 (2026-06-07)

### Added (config — Spring Boot 2.4+ profile expressions)

`Environment.accepts_profiles` (and `@profile` / `conditional_on_*` profile checks) now
support the full Spring Boot 2.4+ profile expression grammar — boolean operators `&`
(AND), `|` (OR), `!` (NOT), and `()` grouping — e.g. `"prod & cloud"`, `"prod | qa"`,
`"(prod & cloud) | qa"`, `"!(dev | test)"`. Parsed via a safe AST evaluator (no `eval`).
The legacy comma-OR (`"dev,test"`) and simple/negated forms remain supported.

## v26.06.38 (2026-06-07)

### Added (observability — OpenTelemetry distributed-trace propagation)

Tracing previously never propagated context: inbound `traceparent` was ignored (so
`@span` started a new root trace), nothing was injected outbound, and logs had no
trace/span IDs. Now a trace flows across service boundaries (no-op without the
observability extra):

- **Inbound**: a new `TracingFilter` (wired into both the Starlette and FastAPI filter
  chains) extracts the W3C trace context from request headers and opens a SERVER span as
  its child — so every span and log line during the request joins the upstream trace.
- **Outbound**: the httpx client injects the current trace context (`traceparent`) into
  outgoing request headers.
- **Logs**: structlog records now carry `trace_id` / `span_id` of the active span (the
  SLF4J MDC equivalent), for both structlog-native and foreign stdlib records.
- New `pyfly.observability.propagation` helpers: `extract_context`, `inject_headers`,
  `current_trace_ids`.

---

## v26.06.37 (2026-06-07)

### Added (security — full method-security SpEL)

Method-security expressions (`@pre_authorize` / `@post_authorize` / `@secure(expression=...)`)
were a narrow regex-based subset (`hasRole` / `hasAnyRole` / `hasPermission` / bare
`isAuthenticated`). They now run through a proper AST-based Spring-Security SpEL evaluator
(`pyfly.security.expression`):

- Full vocabulary — `hasRole`, `hasAnyRole`, `hasAuthority`, `hasAnyAuthority`,
  `hasPermission` (1- and 2-arg), `isAuthenticated`, `isAnonymous`, `permitAll`, `denyAll`
  (each usable bare or called, e.g. `isAuthenticated` or `isAuthenticated()`).
- **`principal` / `authentication`** references with attribute access, **method-argument
  references `#paramName`** (`@pre_authorize`), and **`returnObject`** (`@post_authorize`) —
  enabling ownership/ABAC rules like `@pre_authorize("#ownerId == principal.user_id")`.
- Boolean operators, comparisons, and `in`/`not in`.

Safe by construction (parsed with `ast`, whitelisted nodes, no `eval`, only security
functions callable, dunder attributes rejected). Existing expressions remain valid.

---

## v26.06.36 (2026-06-07)

### Added (resilience — `@retry` + `@circuit_breaker` decorators)

The final parity audit flagged that the starter docs advertised `@retry` and
`@circuit_breaker` as resilience capabilities, but no such decorators existed (retry /
circuit breaking lived only as config-driven behavior inside `pyfly.client` / `pyfly.eda`).
They now exist as standalone decorators in `pyfly.resilience`:

- **`@retry(max_attempts, delay, backoff, max_delay, exceptions)`** — re-invokes on the
  listed exceptions with exponential backoff (capped), re-raising the last exception when
  exhausted; works on sync and async callables. (Spring Retry / Resilience4j `@Retry`.)
- **`@circuit_breaker(breaker)`** with **`CircuitBreaker`** / **`CircuitState`** — a
  thread-safe closed→open→half-open state machine that rejects calls with
  `CircuitBreakerException` while open and recovers via a half-open trial. (Resilience4j
  `@CircuitBreaker`.)

---

## v26.06.35 (2026-06-07)

### Fixed (messaging — Kafka consumer per-message error isolation)

From the final parity audit: the Kafka consume loop awaited each handler with no
per-message guard, so **a single handler exception propagated out of the `async for`
and killed the consumer** — silently halting processing of every subsequent message
(and, under auto-commit, losing it). Each handler invocation is now wrapped: a handler
exception is logged (`kafka_message_handler_failed`) and the consumer continues with
the next message; `asyncio.CancelledError` is re-raised so shutdown still stops the
loop cleanly. (RabbitMQ already isolated per message via `message.process()`.)

---

## v26.06.34 (2026-06-07)

### Fixed (data — @transactional correctness)

From the final parity audit:

- **No partial commit on cancellation/shutdown (silent-failure fix).** The
  `except` branch committed any `BaseException` not listed in `rollback_for`
  (default `(Exception,)`) — so `asyncio.CancelledError`, `KeyboardInterrupt`, and
  `SystemExit` **committed a partially-applied transaction**. A `BaseException` that
  is not an `Exception` now always rolls back, regardless of `rollback_for`.
- **`@transactional(read_only=True)` is now wired** (was dead code): it enters the
  `read_only()` routing scope — so a `RoutingSessionFactory` routes the transaction to
  the read replica — and flags the session (`session.info["read_only"]`).

### Added

- **`@transactional(no_rollback_for=(...))`** — exceptions that should commit rather
  than roll back (Spring's `noRollbackFor`).

---

## v26.06.33 (2026-06-07)

### Fixed (web — FastAPI adapter serialization parity)

The final parity audit found the **FastAPI adapter** (auto-config-preferred) silently
bypassed the entire serialization stack added in v26.06.27/28, so global JSON config,
content negotiation, and RFC 7807 only worked on the Starlette adapter.

- The FastAPI controller now threads `accept` + the message-converter registry into
  `handle_return_value`, so responses honor **`Accept` content negotiation** (JSON/XML,
  q-values) and request bodies are parsed by **`Content-Type`** (incl. XML).
- The FastAPI app now wires `pyfly_json_serializer`, `pyfly_message_converters`, and
  `pyfly_problem_details` onto `app.state` — so the global `pyfly.web.json.*` config and
  **RFC 7807 problem+json** apply on FastAPI too.
- The wiring is now a **shared `install_serialization_state(app, context)`** used by both
  the Starlette and FastAPI adapters, so they cannot drift again.

---

## v26.06.32 (2026-06-07)

### Fixed (DI — two regressions found by the final parity audit)

- **`@bean(primary=True)` now wins when multiple `@bean` methods share a return
  type.** The direct return-type registration used for single-bean resolution was
  created only for the first-processed `@bean` and never updated, so
  `ctx.get_bean(Interface)` could return a non-primary implementation. A later
  `@bean(primary=True)` for the same return type now overwrites it. (The class-level
  `@primary` path was already correct.)
- **`@lazy` beans now run the full init pipeline on first resolution.** A lazily
  created singleton (built post-startup) previously got constructor + field injection
  only — it **skipped `@post_construct`, BeanPostProcessors, and AOP weaving**, so
  `@lazy` beans were never advised by aspects. The container now invokes a
  post-create hook (installed by the `ApplicationContext` after startup) that runs
  BeanPostProcessors + `@post_construct` on lazily-created singletons. (Async
  `@post_construct` on a `@lazy` bean is unsupported in the sync resolution path and
  logs a warning — use an eager bean for async init.)

---

## v26.06.31 (2026-06-07)

### Added (testing — Testcontainers integration)

Wave 4 (final) — the Spring Boot `@Testcontainers` / `@ServiceConnection` equivalent,
in `pyfly.testing.testcontainers`.

- **Container factories** (`postgres_container`, `mysql_container`, `redis_container`,
  `mongodb_container`, `kafka_container`) wrapping `testcontainers` (new optional
  extra `pyfly[testcontainers]`; lazily imported with a clear install hint).
- **`@ServiceConnection`-style auto-wiring**: `pyfly_config_for(container)` maps a
  started container to the right pyfly config keys (Postgres/MySQL → async
  `pyfly.data.relational.url`; Redis → `pyfly.cache.redis.url` + `pyfly.session.redis.url`;
  Mongo → `pyfly.data.document.uri`; Kafka → `pyfly.eda.kafka.bootstrap-servers`), and
  `pyfly_config(*containers)` builds a ready-to-use `Config`.
- **Graceful skip**: `is_docker_available()` + the `@requires_docker` decorator skip
  integration tests cleanly when Docker (or the extra) is absent.

All exported from `pyfly.testing`.

---

## v26.06.30 (2026-06-07)

### Added (core — SpEL-lite expression evaluation)

Wave 4 — a safe subset of Spring's SpEL for the `#{ ... }` form.

- **`@Value("#{ ... }")`** now evaluates expressions: arithmetic, comparison,
  boolean (`and`/`or`/`not`), the ternary `a if c else b`, lists/tuples, literals,
  `${key:default}` config-placeholder substitution (numeric values participate in
  arithmetic), and an `env` mapping — e.g. `Value("#{${pyfly.workers} * 2}")`.
- **`@conditional_on_expression("#{ ... }")`** (`pyfly.context`) — register a bean
  when an expression is truthy (Spring's `@ConditionalOnExpression`), evaluated in the
  config pass at startup.
- **Safe by construction**: expressions are parsed with `ast` and evaluated against a
  whitelist of node types — no attribute access, no function/method calls, no imports;
  `eval` is never used. `pyfly.core.expression.evaluate` / `ExpressionError` /
  `is_expression` are public.

Plain `${key}` placeholders and literal `@Value` strings behave exactly as before.

---

## v26.06.29 (2026-06-07)

### Added (data — read/write datasource routing)

Wave 4 — the Spring `AbstractRoutingDataSource` equivalent.

- **`RoutingSessionFactory`** (`pyfly.data.relational`) routes to a read-replica
  inside a `read_only()` block and to the primary otherwise — selecting by a context
  "lookup key" (the `read_only` contextvar), like `@Transactional(readOnly = true)`.
- **Opt-in via config**: set `pyfly.data.relational.read-replica.url` to enable a
  replica; with none configured the factory always uses the primary (no behavior
  change). Wired as the `routing_session_factory` bean in `RelationalAutoConfiguration`.
- `read_only()` (context manager, nestable), `is_read_only()`, and
  `RoutingSessionFactory` (`.primary()` / `.replica()` explicit accessors) exported
  from `pyfly.data.relational`.

---

## v26.06.28 (2026-06-07)

### Added (web — HTTP message converters / content negotiation, the `HttpMessageConverter` chain)

Completing Jackson parity: serialization is now a pluggable, ordered converter chain
selected by media type for **both** reading and writing — Spring's
`HttpMessageConverter` model.

- **`MessageConverter`** base + **`JsonMessageConverter`** / **`XmlMessageConverter`**,
  both backed by `PyFlyJsonSerializer` (so `pyfly.web.json.*` config applies to every
  format). **`MessageConverterRegistry`** holds them ordered; user-added converters
  take priority (register a `MessageConverterRegistry` bean to add e.g. CBOR or reorder).
- **Real content negotiation**: responses pick a converter from the `Accept` header
  with **q-value** ordering (`default_message_converters` exports JSON+XML, JSON
  default); requests pick a reader from `Content-Type`.
- **XML request bodies** are now deserialized into models (previously XML was
  response-only; requests were JSON-only).
- **`fail-on-unknown-properties`** (from `pyfly.web.json.*`) is enforced on the read
  path via a cached `extra='forbid'` overlay (no user-model mutation).
- All exported from `pyfly.web`. Wired through the controller response path and the
  parameter resolver's body path; both Starlette and FastAPI adapters benefit.

Per-field Jackson features remain Pydantic's domain (`Field(alias=)`,
`@field_serializer`, discriminated unions). Not built: per-route `produces`/`consumes`
constraints (a candidate follow-up) and binary formats beyond a user-registered converter.

---

## v26.06.27 (2026-06-07)

### Added (web — central JSON serialization, the ObjectMapper equivalent)

Spring-parity work, wave 4 (Jackson centralization). Pydantic remains the per-model
engine; this adds what Spring centralizes and pyfly lacked — without cloning Jackson
(no `@JsonView`, no Modules SPI, no `ObjectMapper` god-object, no codegen):

- **Global JSON config** under `pyfly.web.json.*`, applied at one serialization
  boundary: `property-naming-strategy` (`as-is`/`camelCase`), `by-alias`,
  `exclude-none`, `exclude-defaults`, and `fail-on-unknown-properties`. Bound into a
  `JsonProperties` bean.
- **`JsonSerializers` registry** — register an encoder for a **non-Pydantic** arbitrary
  type (e.g. a `Money` value object) so it serializes consistently everywhere
  (provide a `JsonSerializers` bean to customize). The thing Pydantic can't express
  per-model because the type isn't a model.
- **Opt-in `CamelModel`** base — camelCase JSON I/O (accepts snake_case input). No
  global `alias_generator` is injected into user models (that would break validation
  aliases); use this base or the `by-alias` flag.
- **`PyFlyJsonSerializer`** threads through the response path; `JsonProperties`,
  `JsonSerializers`, `PyFlyJsonSerializer`, `CamelModel` are exported from `pyfly.web`.

### Fixed

- **Response serialization of nested/mixed structures.** `_to_json_data` only
  normalized a single `BaseModel` or a list whose *first* element was one — a list of
  dicts, a mixed/heterogeneous list, or a dict containing models/datetimes/UUIDs fell
  through to `json.dumps` and could raise `TypeError`. The serializer now normalizes
  recursively (models, dicts, lists, and common stdlib types).

---

## v26.06.26 (2026-06-07)

### Added (DI — generics-aware injection)

- **Generics-aware injection.** A constructor/field parameter typed as a
  parametrized generic — e.g. `Repository[User, UUID]` — now resolves to the
  registered implementation whose generic bases carry the requested concrete type
  arguments (`class UserRepository(Repository[User, UUID])`). Among a family of
  implementations of the same generic interface, the one matching the type args
  wins; `@primary` breaks ties; a request with no matching parametrization raises
  `NoSuchBeanError`. This supersedes the v26.06.23 note that the parametrized form
  was not auto-matched — it now is, mirroring Spring's `Repository<T, ID>` injection.

---

## v26.06.25 (2026-06-07)

### Added (web — RFC 7807 Problem Details)

Spring-parity work, wave 4 (broader features):

- **Opt-in RFC 7807 `application/problem+json` error responses.** When
  `pyfly.web.problem-details.enabled` is set, the global exception handler emits a
  standard problem detail (`type`/`title`/`status`/`detail`/`instance`, plus
  `code`/`transactionId`/`timestamp`/`context` as extension members) with the
  `application/problem+json` content type. **Default off** — the existing
  `{"error": {...}}` envelope is preserved — mirroring Spring Boot 3's opt-in
  `spring.mvc.problemdetails.enabled`. Applies to both the Starlette and FastAPI
  adapters (they share the handler).

---

## v26.06.24 (2026-06-07)

### Improved (`pyfly.data.Mapper` — the MapStruct equivalent)

Spring-parity work, wave 3. Extended the existing runtime mapper rather than
cloning MapStruct's compile-time codegen:

- **Pydantic-aware.** Field extraction is now shallow and keeps nested models as
  live instances (previously `dataclasses.asdict` deep-flattened nested models to
  dicts, breaking nested destinations). Pydantic models are read via
  `model_fields` and constructed through their validating constructor.
- **Nested-model recursion.** A destination field whose declared type is itself a
  mappable model (dataclass / Pydantic) is now mapped recursively.
- **Collection recursion.** `list`/`tuple`/`set` destination fields of a mappable
  element type are mapped element-wise.
- **Declarative `@mapping` decorator.** `@mapping(Source, Dest, rename=..., transform=..., exclude=...)`
  registers a mapping on the module-level `default_mapper`, so configuration lives
  next to the types. Exported as `pyfly.data.mapping` / `pyfly.data.default_mapper`.

Existing `Mapper` API (`add_mapping`, `map`, `map_list`, projections) is unchanged
and back-compatible.

---

## v26.06.23 (2026-06-07)

### Added (DI / autowiring feature parity)

Spring-parity DI work, wave 2 (feature gaps):

- **Constructor-parameter `@Value`.** Inject a config value into a constructor
  argument via `Annotated[T, Value("${key:default}")]` (Spring's most common
  `@Value` form) — previously `@Value` worked only on fields. The resolved value
  is coerced to the declared type (`int`/`float`/`bool`/`str`).
- **`Provider[T]`** (`pyfly.container.Provider`). Inject a deferred handle and call
  `.get()` (or the instance) for a freshly-resolved bean each time — so a singleton
  can obtain fresh `TRANSIENT` instances, or defer expensive/cyclic beans. The
  Spring `ObjectFactory`/`Provider` equivalent.
- **Map injection.** A `dict[str, T]` constructor/field parameter is injected as
  `{bean-name: bean}` for every named bean assignable to `T` (Spring `Map<String,T>`).
- **`@lazy`** (`pyfly.container.lazy`). Mark a bean lazy-initialized — not created
  during startup, constructed on first resolution instead (Spring `@Lazy`).

Note: generics-aware injection of a parametrized interface (e.g. `Repository[User]`)
remains a deliberate scope decision — inject the concrete repository type; the
parametrized form is not auto-matched.

---

## v26.06.22 (2026-06-07)

### Fixed (DI / autowiring correctness)

Surfaced by a Spring-parity review of the DI container (autowiring front, wave 1):

- **`@Qualifier` now verifies the named bean's type.** `resolve_by_name` /
  `Annotated[T, Qualifier(name)]` / `Autowired(qualifier=...)` looked the bean up
  purely by name and **silently injected a wrong-typed bean**. A qualifier now
  raises if the named bean is not assignable to the declared type (Protocols /
  generics that can't be `isinstance`-checked are still accepted).
- **`list[T]` injection now honors `@order`.** `resolve_all` returned beans in
  binding order, so an injected `list[T]` (e.g. a filter/interceptor chain) was
  unordered — Spring orders injected `List<T>` by `@Order`. Now sorted by `@order`
  (stable within equal order).
- **`@bean` factories can be marked primary.** `bean(primary=True)` records the
  primary flag on the registration so it wins interface resolution among several
  beans (the `@Bean @Primary` equivalent — previously only the class-level
  `@primary` worked, never `@bean` methods).
- **`@bean` methods are profile-filtered.** `bean(profile="...")` is now honored —
  a profile-guarded factory is skipped when the profile is inactive (previously
  only class beans were profile-filtered; `@bean` methods always ran).
- **Cycle detection is thread-safe.** The in-creation set used for
  circular-dependency detection was a single process-wide dict mutated without the
  lock on the transient/request paths, so concurrent resolution could raise
  spurious `BeanCurrentlyInCreationError`. It is now thread-local.

---

## v26.06.21 (2026-06-07)

### Changed (security — behavior change)

- **`HttpSecurity` now denies unmatched requests by default (fail-closed).**
  Previously a request matching none of the configured rules was **allowed
  through** (open-by-default, mitigated only by a build-time warning in
  v26.06.12). It is now **denied with 403**, matching Spring Security 6: once you
  declare authorization rules, undeclared paths are denied unless you explicitly
  permit them. This closes a footgun where a forgotten terminal rule silently left
  paths open.

  **Migration:** add an explicit catch-all or list your public paths —
  `.any_request().permit_all()` to restore the previous open behavior, or
  `.request_matchers("/health", "/docs").permit_all()` for specific public paths.
  An `HttpSecurity` built with **no rules at all remains a no-op** (never a blanket
  lockout). The build-time "no terminal rule" warning is removed (the secure
  default makes it unnecessary).

These surfaced in the `implement-security` audit (v26.06.12), where the open
default was flagged as a Spring-6 deviation; deferred then, now adopted.

---

## v26.06.20 (2026-06-07)

### Fixed

- **`pyfly.testing` is now importable without `jsonpath-ng`.** `pyfly.testing`
  unconditionally imported `client.py`, which imported `jsonpath_ng` at module
  load — but `jsonpath-ng` was only a dev dependency, so any consumer that
  followed a `testing-*` skill (`from pyfly.testing import ...`) without it hit
  `ModuleNotFoundError`. The import is now lazy (only `TestResponse.assert_json_path`
  needs it, with a clear "install `pyfly[testing]`" error), and **a `testing`
  optional-dependency extra** provides `jsonpath-ng`.
- **Scaffolded `todo_service` guards a missing id.** The generated service
  dereferenced `find_by_id` without a None check — a latent `AttributeError` on a
  missing id and a `mypy --strict` `union-attr` error in the data-relational /
  data-document variants. All variants now raise `ResourceNotFoundException`
  (→ 404), and the in-memory repository's `find_by_id` returns `Optional`.

### Added

- **`pyfly[testing]` extra** (`jsonpath-ng`).
- Regression tests: `tests/testing/test_import_without_jsonpath.py` and
  `tests/cli/test_scaffold_todo_service_guard.py`.

These surfaced validating the `testing-async-services`, `testing-cqrs-handlers`,
`testing-sagas`, and `debugging-async-services` skills — all of which validated
faithful (a fresh agent proved saga compensation-on-failure and diagnosed a planted
contextvars bug; every referenced `pyfly.testing` utility exists and works).

---

## v26.06.19 (2026-06-06)

### Fixed

- **WebSocket `on_disconnect` failures are no longer silently swallowed.** The
  Starlette adapter wrapped the `on_disconnect` cleanup hook in
  `contextlib.suppress(Exception)`, so a failing cleanup (leaked resource, lock)
  vanished without a trace. Failures are now logged (`warning` + traceback),
  matching the handler-error path.
- **`on_disconnect` runs only when the connection was accepted.** It previously
  fired unconditionally in `finally`, so a handler that raised before
  `session.accept()` got a spurious disconnect for a never-completed handshake.
  `WebSocketSession` now tracks `accepted`, and the adapter gates the hook on it.
- **`WebSocketHandler` protocol docstrings corrected.** They implied `on_connect`
  and `on_message` were auto-dispatched by the framework; they are **not** (only
  `on_disconnect` is). The `@websocket_mapping` method owns the full lifecycle
  (accept + receive loop). Implementing `on_connect`/`on_message` and expecting
  the framework to call them was a silent no-op; the docstrings now state they are
  caller-invoked. (The `implement-websocket` skill already documented this
  correctly.)

### Added

- **`WebSocketSession.accepted`** property.
- **`tests/websocket/` suite (5 tests)** — the module was previously untested.
  Covers message flow, disconnect cleanup, the accept-gating + error-logging
  fixes, the `accepted` flag, and the on_message-not-auto-dispatched contract.

### Notes

- Documented that the WebSocket controller instance is a **singleton shared
  across all connections** — keep per-connection state on the `WebSocketSession`,
  never on `self`.

These surfaced in an audit while validating the `implement-websocket` skill (which
validated clean — messages flow, broadcast, and disconnect cleanup all proven).

---

## v26.06.18 (2026-06-06)

### Tests

- **Expanded `rule_engine` evaluator coverage** (8 → 44 tests). Validating the
  `implement-rule-engine` skill found **no bug** — the evaluator is correct — but
  the module had only ~8 tests for 361 lines, with many paths unexercised. Added
  `tests/rule_engine/test_evaluator_coverage.py` locking in: every leaf operator
  (`eq/ne/gt/ge/lt/le/in/not_in/regex`) including None/missing-field safety and a
  type-mismatch surfacing, composite `and`/`or`/`not` (+ `not`-arity), `then` vs
  `otherwise`, `set`/`increment`/nested-write/unsupported-action isolation, the
  loud unknown-operator error, the disabled-rule skip, and `RuleSet` priority
  ordering + cross-rule error isolation. No framework behavior changed.

---

## v26.06.17 (2026-06-06)

### Fixed

- **A synchronous `@scheduled` task no longer blocks the event loop.**
  `TaskScheduler._invoke` ran the scheduled method inline on the loop, so a sync
  task with a blocking body (I/O, `time.sleep`, a blocking DB/HTTP call) stalled
  the entire application for its duration — every request and every other task.
  Sync methods are now offloaded to a worker thread via `asyncio.to_thread`;
  async methods are still awaited on the loop. (Confirmed: a sync task with
  `time.sleep(0.4)` previously froze a 20 ms heartbeat to a single tick over
  0.2 s; it now keeps ticking.)

This surfaced in an audit while validating the `implement-scheduling` skill (which
validated clean — tasks fire repeatedly, errors are isolated, shutdown is
graceful, and the cron next-run is correct via croniter).

---

## v26.06.16 (2026-06-05)

### Fixed

- **The `hexagonal` archetype now actually wires its ports.** The generated app
  defined inbound (use-case) and outbound (repository) ports that **nothing
  implemented** — `resolve(TodoRepositoryPort)` raised `NoSuchBeanError`, the
  ports were dead code, and `TodoService`'s docstring "Implements the inbound
  ports" was false (the DI scanner binds ports by MRO, so an adapter must inherit
  the port). Now the application service implements the four inbound use-case
  ports and the in-memory adapter implements the outbound repository port, so both
  resolve and the architecture is genuinely hexagonal. The use-case boundary is
  async across all variants (in-memory / relational / document) for consistency.
- **Generated hexagonal code is `mypy --strict`-clean and handles not-found.** The
  data-relational / data-document variants dereferenced `find_by_id()`'s
  `T | None` result without a check (a `mypy` error and a latent `AttributeError`
  on a missing id); they now raise `ResourceNotFoundException` (→ 404).
- **DTO id type fixed.** `TodoResponseDTO.id` was `int` in the relational variant
  while the domain `Todo.id` is always `str`; it is now `str` in every variant.

These surfaced in an audit while validating the `implement-hexagonal-adapter` skill
(which validated clean — DI resolves a Protocol/ABC outbound port to its adapter,
the keystone capability for the pattern; zero/multiple-implementation cases raise
clear `NoSuchBeanError` / `NoUniqueBeanError`).

---

## v26.06.15 (2026-06-05)

### Fixed

- **`CacheManager` now satisfies the `CacheAdapter` protocol.** It implemented
  only `get`/`put`/`evict`/`clear`, so `isinstance(mgr, CacheAdapter)` was `False`
  and passing one as a `@cacheable` backend raised `AttributeError: 'CacheManager'
  object has no attribute 'exists'` on the first null-cached hit. Added the
  missing `exists` / `put_if_absent` / `evict_by_prefix` / `start` / `stop`
  (mirrored to both primary and fallback).
- **Cache decorators reject a sync target with a clear error.** `@cacheable` /
  `@cache_evict` / `@cache_put` await an async backend, so decorating a sync
  function used to fail with a cryptic `await` `TypeError` at call time; it now
  raises a clear `TypeError` at decoration time (cache adapters are async-only).
- **Bad key templates raise a clear `ValueError`.** A `{param}` template
  referencing a name not in the function signature raised a bare `KeyError` at
  call time; it now names the unknown parameter.

### Added

- **`InMemoryCache(max_size=...)` with LRU eviction.** The in-memory adapter was
  unbounded (the advertised `max_size` stat was always `None`). It now accepts an
  optional `max_size` (wired from `pyfly.cache.max-size`) and evicts the
  least-recently-used entry on overflow; the default remains unbounded.

### Notes

- Documented two by-design properties surfaced by the audit: cache **keys are
  namespaced by the backend instance + key template** (reuse the same template
  across methods only for the same logical entry — that is what lets
  `@cache_evict` invalidate a `@cacheable` entry), and the **Redis JSON
  round-trip is lossy** (a cached Pydantic model returns as a `dict` on a Redis
  hit, unlike the in-memory adapter).

These surfaced in an edge-case audit while validating the `implement-cache-strategy`
skill (which validated clean — cache hits skip the source and evict invalidates).

---

## v26.06.14 (2026-06-05)

### Fixed

- **Validator-raised errors now return 422, not a bare 500.** When a Pydantic
  `field_validator`/`model_validator` raised `ValueError` — which is exactly how
  the framework's own `valid_iban` / `valid_currency_code` markers work — Pydantic
  embedded the raw `ValueError` in the error `ctx`. The global handler dumped the
  exception context verbatim and `json.dumps` crashed, so the client got an empty
  500 instead of the structured 422. The error envelope now coerces the context to
  a JSON-safe form (stringifying anything non-serializable), so it always renders.
- **`is_valid_amount` no longer crashes on non-finite floats.** `inf` / `-inf` /
  `NaN` raised `OverflowError`/`ValueError` (surfacing as a 500 inside a validator);
  they are now rejected as invalid.
- **`@validator` / `@validate_input` work on sync functions.** Both unconditionally
  `await`-ed the target, so decorating a synchronous function raised
  `TypeError: object ... can't be used in 'await' expression`. They now adapt to the
  target (sync stays sync, async stays async).
- **`@validate_input` no longer silently skips non-dict input.** A value that was
  neither a `dict` nor a model instance passed through unvalidated; it is now
  rejected with a `ValidationException`.
- **Visa card pattern tightened** to valid lengths (13/16/19 digits) instead of
  also accepting 14/17.

These surfaced in an audit while validating the `implement-validation` skill (which
itself validated clean — invalid input is genuinely rejected). The IBAN mod-97 and
Luhn algorithms were confirmed correct.

---

## v26.06.13 (2026-06-05)

### Security

- **Session fixation fixed.** Authentication now rotates the session id, so an
  attacker who fixed a victim's pre-auth `PYFLY_SESSION` id cannot ride the
  authenticated session. New `HttpSession.rotate_id()` (preserves data, records
  `previous_id`); `SessionFilter` migrates the store entry and re-issues the
  cookie under the new id; the OAuth2 login flow calls it on successful login.
- **Session cookie `Secure` auto-set over HTTPS.** `SessionFilter` now marks the
  cookie `Secure` when the request arrives over HTTPS (honoring
  `X-Forwarded-Proto`) even if not explicitly configured — hardening production
  without breaking plain-HTTP local development.
- **Redis session deserialization hardened.** `RedisSessionStore` rehydrated
  *any* tagged type via `importlib` + `obj(**payload)` — an arbitrary-object
  instantiation gadget if the store were ever attacker-writable. Rehydration is
  now restricted to an allowlist (`SecurityContext` pre-registered); other tagged
  values return a plain dict. Apps opt custom dataclasses in via
  `allow_session_type()`.

### Added

- **`tests/session/` suite (16 tests)** — the session subsystem was previously
  untested. Covers `HttpSession` (incl. rotation), `InMemorySessionStore`
  (incl. TTL expiry), `SessionFilter` (new/existing/invalidate/rotation/secure
  auto-detect), and `RedisSessionStore` (SecurityContext round-trip + the
  allowlist gadget guard).

Completes the session-hardening follow-up deferred from v26.06.12.

---

## v26.06.12 (2026-06-05)

### Security

- **JWT now requires `exp`.** `JWTService.decode` (and the JWKS resource-server
  validator) verified `exp` only when present, so a token minted with **no `exp`**
  was accepted and never expired. `decode` now requires `exp`
  (`options={"require": ["exp"]}`), and `JWTService.encode` auto-adds an `exp`
  (now + `expiration_seconds`, default 3600) when the payload omits one — so every
  issued token expires.
- **OAuth2 client secret compared in constant time.** `AuthorizationServer`
  compared the client secret with `!=` (a timing side-channel); it now uses
  `secrets.compare_digest`.
- **OAuth2 grant-type confusion fixed.** The token endpoint ignored the client's
  registered `authorization_grant_type`, so a client registered for
  `authorization_code` could mint `client_credentials` tokens. The
  `client_credentials` grant now requires the client to be registered for it
  (otherwise `UNAUTHORIZED_CLIENT`); server-unsupported grants still return
  `UNSUPPORTED_GRANT_TYPE`.
- **`HttpSecurity` footgun warning.** `build()` now logs a warning when
  authorization rules are configured without a terminal `any_request()` rule
  (paths matching no rule fall through allowed) — recommending
  `.any_request().deny_all()` / `.authenticated()`.

These surfaced in an adversarial security audit while validating the
`implement-security` skill (which itself validated clean — enforcement proven,
no silent auth bypass). Session-subsystem hardening (session-fixation id
rotation, `secure` cookie default, Redis-deserialization allowlisting) plus a
dedicated `tests/session` suite are tracked as a focused follow-up.

---

## v26.06.11 (2026-06-05)

### Fixed

- **Scaffolded web entry points are now `mypy --strict`-clean.** A generated
  `main.py` (web-api / SSR web archetypes) had an **untyped** lifespan
  (`async def _lifespan(app):`) and assigned to **undeclared private**
  `PyFlyApplication` attributes (`_route_metadata`, `_docs_enabled`, `_host`,
  `_port`), so a scaffolded project that enabled `mypy --strict` — which PyFly's
  conventions mandate — failed type-checking on code it never wrote. The lifespan
  is now fully annotated (`app: Starlette) -> AsyncIterator[None]`) in both entry
  templates, and those four web-runtime fields are declared (typed) on
  `PyFlyApplication`. A freshly scaffolded web project now passes `mypy --strict`
  on `main.py`. (The `implement-web-controller` skill itself validated clean,
  including confirming the v26.06.07 Annotated request-binding markers keep user
  controllers strict-clean.)

---

## v26.06.10 (2026-06-05)

### Fixed

- **`SqlAlchemyEventStore` optimistic-concurrency hole.** `append()` read the
  current version via `latest_version()` on a **separate connection before** the
  write transaction, so two concurrent writers could both pass the
  `expected_version` check; the loser then violated `UNIQUE(aggregate_id,
  sequence)` and surfaced a **raw `IntegrityError`** — never the documented
  `ConcurrencyError` — so retry-on-`ConcurrencyError` callers missed the
  collision. The version check now runs **inside** the write transaction, and a
  `UNIQUE` violation is translated to `ConcurrencyError`. (`InMemoryEventStore`
  was already correct — atomic under its lock.)
- **`EventUpcaster` was dead code.** `EventUpcaster`/`NoOpUpcaster` were exported
  and documented but **never invoked** by any read path. Both event stores now
  accept `upcasters=...` and apply them in `load()` and `stream_all()`, so stored
  events are upcast to the current schema on read (default: no upcasters → identity).

### Changed

- **`EventHandlerException` is now exported from `pyfly.eventsourcing`** (it was
  only reachable via the private `pyfly.eventsourcing.aggregate` submodule, unlike
  its sibling `ConcurrencyError`). Users can now `from pyfly.eventsourcing import
  EventHandlerException` to catch missing-handler failures.
- **`TransactionalOutbox.dead_letters()`** added — surfaces records that exhausted
  `max_attempts` (retained, but excluded from `pending()`) for inspection / manual
  retry.

---

## v26.06.09 (2026-06-05)

### Fixed

- **Startup `wiring_summary` now reports EDA event listeners.** The bean/wiring
  summary logged at startup surfaced `event_listeners` (`@app_event_listener`),
  `message_listeners`, `cqrs_handlers`, `scheduled_tasks`, `async_methods`, and
  `post_processors` — but **omitted EDA `@event_listener` subscriptions**
  (tracked under `event_listeners_eda`). So a service that wired EDA listeners saw
  them reported as absent in the summary, which is misleading when using the
  summary to diagnose noop-wiring (as `debugging-async-services` recommends). The
  EDA listeners were correctly subscribed and dispatched — only the summary line
  under-reported them. The summary now includes `event_listeners_eda`; the field
  assembly was extracted to a testable `pyfly.core.application._wiring_summary_fields`
  helper with regression tests. No behavioural change to event subscription or
  dispatch. Found while validating the `implement-eda` skill (the skill and the
  eda/messaging runtime were already correct).

---

## v26.06.08 (2026-06-05)

### Fixed

- **Data-backed scaffolds now ship passing tests.** `pyfly new <name> --features
  data-relational` (or `data-document`) generated an **async, DB-backed**
  `TodoService`/`TodoRepository`, but the CLI's `test_todo_service.py.j2` template
  emitted only **synchronous** tests that called those async methods without
  `await` (and constructed the repository with no session) — so a freshly
  scaffolded data project failed `pytest` out of the box with 5 errors
  (`coroutine object has no attribute …` / `object of type coroutine has no
  len()`). The test template now branches on the selected feature: a real
  in-memory SQLite async test for `data-relational`, a fast async test with a
  mocked repository for `data-document`, and the original synchronous in-memory
  test otherwise. Added regression tests (`tests/cli/test_scaffold_todo_tests.py`)
  asserting data-backed scaffolds emit async tests and the plain scaffold stays
  synchronous; verified end-to-end that a freshly generated `data-relational` and
  `data-document` project each pass `pytest` (5 passed) out of the box. The
  `data` runtime, the data adapters, and the `implement-data-repository` skill
  were already correct — this was purely a scaffold-generator (CLI template) bug.

---

## v26.06.07 (2026-06-05)

### Fixed

- **Web request-binding markers are now type-checker transparent.** `PathVar`,
  `QueryParam`, `Body`, `Header`, `Cookie`, `File`, and `Valid` (in
  `pyfly.web.params`) were plain `Generic[T]` classes, so `mypy --strict` saw a
  handler parameter `order_id: PathVar[str]` as a `PathVar[str]` object rather
  than `str` — meaning user controllers written exactly as the docs show could
  not pass strict type-checking without `# type: ignore`/`cast`. The markers are
  now `Annotated[T, <sentinel>]` aliases, so a type checker sees `PathVar[str]`
  as `str` and `Valid[Body[Order]]` as `Order`, while the binder recovers the
  source from the annotation metadata at runtime via the new
  `pyfly.web.params.inspect_binding`. Runtime binding semantics are unchanged
  (path/query/body/header/cookie/file resolution, `Valid[...]` validation, and
  the `Valid[Model]` → validated-body shorthand all behave identically).
  Internally removed the now-redundant `cast(...)`/`# type: ignore` workarounds
  in `idp/web.py` and `transactional/rest/controllers.py`. The whole tree
  (`mypy src/pyfly`, 607 files) and the full suite stay green.

---

## v26.06.06 (2026-06-05)

### Fixed

- **Web services boot without the `security` extra.** Both web adapters
  (`pyfly.web.adapters.starlette` and `pyfly.web.adapters.fastapi`) collected
  OAuth2 login routes during `create_app()` via an **unconditional**
  `from pyfly.security.oauth2.login import OAuth2LoginHandler`. That module imports
  `pyjwt` at load time, so a `pyfly[web]`-only install (no `pyfly[security]`) —
  exactly what `pyfly new --archetype web-api` / `fastapi-api` produces — crashed
  at app-build time with `ModuleNotFoundError: No module named 'jwt'`. The import is
  now lazy and guarded (`try/except ImportError`): a web-only service boots cleanly,
  and OAuth2 login routes still mount when `pyfly[security]` is installed and an
  `OAuth2LoginHandler` bean is registered. Regression test:
  `tests/web/test_web_only_boot.py`.

---

## v26.06.05 (2026-06-05)

### Fixed

- **`RateLimiter` sync path is now thread-safe.** The token bucket was guarded by
  an `asyncio.Lock`, but the `@rate_limiter` **sync** decorator mutated the bucket
  with no lock at all, so concurrent threaded calls could over-consume tokens
  (race on the read-modify-write of `_tokens`). The bucket now uses a
  `threading.Lock`, and both the async (`acquire`) and sync decorator paths go
  through one locked `_try_acquire()`, so a limiter shared across async tasks and
  sync/threaded callers stays consistent. Same class of fix as the v26.06.01
  Bulkhead change.

---

## v26.06.04 (2026-06-05)

### Documentation accuracy pass

A comprehensive sweep of the docs against the current codebase — no functional
changes.

- **New guide:** `docs/modules/session.md` (HTTP session store, `SessionFilter`,
  in-memory/Redis stores, `pyfly.session.*`) — previously only mentioned inside
  the security guide.
- **README** now advertises the unified structured logging, Spring-style logging
  configuration, and on-by-default **PII redaction** (with the optional
  `pyfly[pii]` / Presidio upgrade); the `pii` extra is listed in the installation
  guide.
- **Indexes refreshed** (`docs/index.md`, `docs/README.md`,
  `docs/modules/README.md`, `docs/adapters/README.md`) so every module/adapter
  guide is linked; fixed the Logging link (was pointing at the observability
  guide).
- **Corrected factual drift across ~37 guides** to match the code, e.g.: the five
  always-active web filters and their ordering; `SecurityException` → HTTP 403;
  relational pool keys (`pool.size`/`max-overflow`/…); `@message_listener`
  `group=`; actuator `beans` `contexts` envelope and the `threaddump`/`prometheus`
  endpoints; health statuses (`UP`/`UNKNOWN`/`OUT_OF_SERVICE`/`DOWN`); `@timed`/
  `@counted`/`@span` work on sync and async; AOP advice sync/async semantics;
  DI error types (`NoSuchBeanError`/`NoUniqueBeanError`/`BeanCurrentlyInCreationError`)
  and the `Registration.factory` field; `Query.get_cache_key` SHA-256; the IDP
  provider/route surface; notification provider adapters; and more.

---

## v26.06.03 (2026-06-05)

### Presidio PII path — now functional + CI-covered

- **`PresidioRedactor` now actually uses Presidio.** It previously passed pyfly's
  regex-oriented entity names (`EMAIL`, `IBAN`, `PHONE`…) to Presidio, whose
  recognizers use different names (`EMAIL_ADDRESS`, `IBAN_CODE`, `PHONE_NUMBER`…),
  so detection found almost nothing and always fell back to regex. It now detects
  with Presidio's **full recognizer set** (including NER for free-text **names**,
  locations, etc.) and then runs the regex pass over the result, so token-types
  Presidio has no recognizer for (JWT, bearer tokens, URL credentials) are still
  masked.
- **Configurable spaCy model** — new `pyfly.logging.redaction.presidio.model`
  (default `en_core_web_lg`). Set a lighter model (e.g. `en_core_web_sm`) where the
  full model is too heavy. If the model isn't installed, redaction falls back to
  regex rather than failing.
- **Opt-in CI job** (`.github/workflows/pii.yml`, `PII / Presidio`) installs
  `pyfly[pii]` + a small spaCy model and exercises the Presidio NER path
  end-to-end. It runs on manual dispatch and automatically only when the redaction
  code changes — the main CI stays fast (it excludes the heavy `pii` extra).

---

## v26.06.02 (2026-06-05)

### Unified logging, Spring-style configuration & PII redaction

A logging overhaul that intercepts and uniformly formats **every** logger, adds
Spring-style file-based configuration, and redacts PII by default.

- **Unified interception & formatting.** All loggers — framework, third-party
  libraries (uvicorn, sqlalchemy, kafka, httpx…), and anything on stdlib
  `logging` — now render through a single formatter, so output shares one
  timestamp/level/structure. The `StructlogAdapter` uses structlog's
  `ProcessorFormatter` + `foreign_pre_chain`; the stdlib fallback uses a matching
  `Formatter`. Previously third-party logs bypassed the framework format
  (bare `%(message)s`).
- **Spring-style configuration.** New `pyfly.logging.*` keys: `format`
  (`console`|`json`|`logfmt`), `pattern.console`/`pattern.file` (logback-style
  layout tokens), `file.name`/`file.path` (file appender), `rolling.*`
  (size-based rotation), and `config` — an external `logging.yaml` (dictConfig)
  or `logging.ini` (fileConfig) escape hatch. Existing `pyfly.logging.level.*` /
  `format` keys are unchanged.
- **PII redaction.** PII is masked in every log record by default via a fast
  built-in regex engine (email, credit-card [Luhn-validated], IBAN, US SSN, JWT,
  bearer tokens, URL credentials, phone; IPv4/IPv6 available but off by default).
  Configurable via `pyfly.logging.redaction.*` (`enabled`, `engine`
  `regex`|`presidio`|`auto`, `entities`, `mask` `placeholder`|`partial`|`hash`,
  `deny-fields`/`allow-fields`, `extra-patterns`). Installing the new
  **`pyfly[pii]`** extra auto-upgrades detection to Microsoft Presidio
  (`engine: auto`); any Presidio failure falls back to regex so a logging
  misconfiguration never crashes the app. An opt-in `redaction.streams.enabled`
  wraps `stdout`/`stderr` to mask raw `print()`/direct writes (the rich CLI
  console is bypassed).

### Docs

- Rewrote the logging guide for the above. Design spec + implementation plan
  added under `docs/superpowers/`.

---

## v26.06.01 (2026-06-05)

### Full-framework parity & wiring remediation

A framework-wide audit against the Java Firefly Framework surfaced ~130 verified
issues — mostly *noop-wiring* (a capability was built but never connected) and
parity gaps. This release fixes them. Most changes are bug fixes or new
auto-wiring; behavior changes that could affect existing apps are called out as
**[behavior]**.

**Web**

- **[behavior]** A missing **required** `QueryParam`/`Header`/`Cookie` (no default
  and a non-`Optional` type) now returns **HTTP 400** (`MISSING_PARAMETER`) instead
  of silently binding `None`. Make a parameter optional with a default or an
  `X | None` type.
- **CORS** is now auto-configured from `pyfly.web.cors.*` (`enabled`,
  `allowed-origins`, `allowed-methods`, `allowed-headers`, `allow-credentials`,
  `exposed-headers`, `max-age`) — secure-by-default disabled, like Spring's
  `CorsAutoConfiguration`. No more hand-passing a `CORSConfig` to `create_app`.
- The `ExceptionConverterService` is now wired into the global error handler:
  non-PyFly exceptions (Pydantic, JSON, `TimeoutError`, plus user `@bean`
  converters) are translated to the right HTTP status before responding.

**Configuration & config-server**

- `get()` and `${...}` placeholder references now use **relaxed** kebab/snake
  matching (`${my-prop.sub-key}` resolves a value stored under `my_prop.sub_key`),
  consistent with `bind()`.
- An **env-only** `PYFLY_<PREFIX>_*` variable with no file entry now binds to a
  `@config_properties` field.
- **Remote config import**: when `pyfly.cloud.config.uri` (or
  `pyfly.config.import`) is set, the app fetches config from a config server at
  startup and merges it at high precedence (non-fatal unless
  `pyfly.cloud.config.fail-fast=true`).
- The **config server now serves HTTP**: `GET/POST /{application}/{profile}[/{label}]`
  and `GET /_list` are mounted when `pyfly.config-server.enabled=true`; `fetch`
  returns the full Spring-Cloud-Config overlay set (app/profile → app/default →
  application/profile → application/default); the filesystem backend root is
  configurable and persistent via `pyfly.config-server.backend.root`.

**Admin dashboard**

- **[behavior]** `pyfly.admin.require-auth` is now **enforced** on every
  `/admin/api/*` route (401 unauthenticated, 403 missing every role in
  `pyfly.admin.allowed-roles`); the SPA shell and static assets stay public.
- **Server mode** (`pyfly.admin.server.enabled`) wires an instance registry,
  mounts `/admin/api/instances`, and reports `serverMode=true`.
- **Client self-registration**: with `pyfly.admin.client.url` +
  `auto-register=true`, the app registers with a remote admin server at startup
  and deregisters at shutdown.
- Selecting the `TRACE` or `OFF` logger level now applies instead of silently
  failing; a beans SSE stream is served at `/admin/api/sse/beans`.

**Observability & actuator**

- Distributed tracing now **exports** spans (the `TracerProvider` previously had
  no span processor, so every `@span` was discarded). Configure via
  `pyfly.observability.tracing.exporter` (`otlp`|`console`|`none`) or the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT`.
- `/actuator/threaddump` reports the correct `className` (module) / `methodName`;
  `/actuator/prometheus` degrades gracefully (503) when `prometheus_client` is
  absent instead of raising.

**Starters**

- Fixed the property keys the `@enable_*_stack` bundles set so the bundled
  adapters actually activate: `pyfly.data.relational.enabled` /
  `pyfly.data.document.enabled`, `pyfly.eda.provider=auto`,
  `pyfly.security.enabled`, and `pyfly.web.actuator.enabled` (the previous keys
  were read by nothing).

**ECM**

- Storage and e-signature adapters are now selected from config
  (`pyfly.ecm.storage.provider` = `local`|`s3`/`aws`|`azure`;
  `pyfly.ecm.esignature.provider` = `noop`|`docusign`|`adobe`|`logalty`) — only
  local + noop were ever wired before. `DocumentService.delete` now reports
  storage-delete failures.

**Plugins**

- Extension points are now registered and type-validated
  (`register_extension_point`/`has_extension_point`; extensions are checked against
  their declared `@extension_point` class). Unloading a plugin
  (`PluginManager.remove`/`unload_all`) now unregisters its extensions.

**Orchestration / resilience / i18n / shell / testing / CLI**

- Orchestration REST: added `GET /api/orchestration/dlq/count`; **[behavior]**
  `GET /api/orchestration/executions` with no `status` now returns in-flight
  executions only.
- `Bulkhead` now uses a single lock-guarded permit counter shared by sync and
  async calls, so a shared bulkhead can no longer desynchronise.
- i18n message substitution now honors `java.text.MessageFormat` quoting
  (`''` → `'`, single-quoted text is literal).
- `@shell_option(type=…, choices=…)` / `@shell_argument(type=…)` overrides are now
  honored by parameter inference.
- `mock_bean(...)` descriptors are now injected into the test `ApplicationContext`,
  so DI-resolved collaborators receive the mock.
- `pyfly new` now honors the package name entered in the interactive wizard.

### Docs

- New module guides for **i18n** and **WebSocket**; updated guides for web,
  configuration, config-server, admin, observability, actuator, ECM, plugins,
  resilience, transactional, validation, shell, testing, and CLI to match the
  changes above; doc indexes refreshed.

---

## v26.06.00 (2026-06-04)

### Spring Boot parity — observability, actuator & configuration

Brings pyfly's observability, actuator, and configuration to drop-in Spring Boot
parity: identical Micrometer metric names, the full `/actuator/*` endpoint surface,
and Spring-style YAML config semantics (Spring's `management.*` key structure under
the `pyfly.*` namespace; legacy keys still work).

- **Observability.** HTTP auto-instrumentation now emits Micrometer's
  `http_server_requests_seconds` (count/sum + `_max` gauge; optional histogram)
  tagged `method`/`uri` (templated, cardinality-safe)/`status`/`outcome`/`exception`.
  The metrics filter is wired directly in `create_app` (it was previously a bean
  built too late to ever join the chain, so HTTP metrics were silently never
  collected). Process/system meters use Micrometer names (`process_uptime_seconds`,
  `process_cpu_usage`, `system_cpu_count`, …). `@timed`/`@counted` follow Micrometer
  naming + tags (`class`/`method`/`exception`, `result` for counters).
- **Actuator.** On by default with Spring-exact secure exposure
  (`pyfly.management.endpoints.web.exposure.include`, default `health,info`),
  configurable base-path, and a now-registered `/actuator/prometheus`
  (pinned to `version=0.0.4`). `/actuator/metrics` returns Micrometer JSON
  (dot names, `COUNT`/`TOTAL_TIME`/`MAX`, `availableTags`, `?tag=` drill-down).
  New endpoints: `configprops`, `mappings`, `scheduledtasks`, `threaddump`,
  `caches`, `conditions`, `httpexchanges`; `/actuator/beans` uses the `contexts`
  envelope; loggers use the Spring shape (`WARN`/`OFF`, `GET`/`POST /loggers/{name}`,
  groups). The Starlette and FastAPI adapters share one wiring path.
- **Health.** Severity-based status aggregation (`UP`/`UNKNOWN`/`OUT_OF_SERVICE`/`DOWN`),
  503 for down states, `show-details`/`show-components` config, `/health/{component|group}`.
  The DB and CQRS health indicators are now registered and conform to the protocol.
- **Configuration.** Relaxed binding (kebab→snake), env-var overrides visible to
  `@config_properties` binding and type-coerced, ordered property sources, and secret
  masking (incl. URI userinfo passwords). The admin Configuration/Environment views
  are sorted, grouped, source-attributed, and masked; the admin Metrics view uses the
  Prometheus names and is now SSE-push driven.
- **Fixes.** mongomock + beanie 2.x test compatibility shim; `install.sh` now probes
  version-suffixed interpreters (`python3.13`/`3.12`) so it no longer aborts when the
  bare `python3` is an older build.

## v26.05.12 (2026-05-31)

### Admin dashboard — responsive cards, fullscreen, navbar polish

- **Fullscreen / expand cards.** New reusable affordance: the **Bean Graph** and
  **Log Viewer** cards gain a maximize button that expands them to fill the
  viewport (Escape to exit) — ideal for exploring a large dependency graph or a
  busy log stream. The expanded card re-parents to the document root so it sits
  above the page (the ⌘K palette, toasts and the bean detail panel still layer
  correctly on top).
- **Responsive sizing.** The dependency graph now scales with the viewport
  (was a fixed 600px) and re-lays out on window resize / fullscreen toggle; the
  log output grows with the window instead of a fixed 600px cap.
- **Bean Graph** also gains a **Reset view** control (clears pan/zoom).
- **Loggers** view: an **Effective Level Distribution** bar (share of each level)
  and the table now scrolls within a viewport-relative height with a sticky header.
- **Metrics** view: the metric-list card now stretches to the full height of the
  detail panel (was capped at 520px) and scrolls internally, so it uses the
  available vertical space.
- **Navbar & sidebar.** The sidebar brand is now exactly the navbar height so the
  two top bars line up across the split; the brand shows the pyfly wordmark only.
  The navbar gains a live auto-refresh status pill and tidier, grouped controls.

---

## v26.05.11 (2026-05-31)

### Admin dashboard — loading skeletons & consistent empty states

A cross-cutting polish pass over every view:

- **Skeleton loaders.** While a view fetches its data it now shows a shimmer
  skeleton that mirrors the eventual layout (stat cards + table/cards) instead of
  a bare spinner, so the page doesn't jump and loads feel faster. New reusable
  `components/skeleton.js` (`pageSkeleton`, `skeletonStatCards`, `skeletonTable`,
  `skeletonCard`, `skeletonLine`); theme-aware sheen; honours
  `prefers-reduced-motion`.
- **Consistent empty / error states.** "No data" and "failed to load" panels are
  now a single iconographic component (`components/empty-state.js` —
  `createEmptyState` / `createEmptyStateCard`) with a fitting icon, a clear title
  and a helpful sentence, replacing the ad-hoc title+text blocks scattered across
  views. Errors use a danger-tinted alert icon and preserve the error message.
- Applied across all 17 views; behaviour is otherwise unchanged (data handling,
  SSE/chart lifecycle and cleanup are untouched). Verified live across every view
  with zero console errors.

---

## v26.05.10 (2026-05-31)

### Admin dashboard — HTTP request analytics on Traces

- The **Traces** view now leads with **live request analytics** computed from the
  trace stream:
  - **Stat cards**: Total Requests, Avg Duration, **Error Rate** (4xx+5xx %, tinted
    amber/red when elevated), and Max Latency.
  - **Status Mix**: a segmented bar + legend showing the 2xx/3xx/4xx/5xx split with
    counts and percentages.
  - **Latency Distribution**: a histogram across latency buckets (<10 ms … ≥1 s)
    plus a **p50 / p90 / p95 / p99** percentile strip.
- Everything updates live as requests arrive (debounced) and resets on **Clear**.
- The client trace buffer is now **bounded to 500 entries** (matching the server
  ring buffer): the in-memory array, the table DOM and the per-refresh analytics
  cost no longer grow without bound on a long-lived dashboard tab.
- Responsive (cards/charts stack and resize on mobile, dark + light themes) and
  accessible (the decorative mix bar is `aria-hidden`; the legend carries the
  numbers). Avg Duration reads `--` (not `0.0 ms`) when no trace carries a duration.

---

## v26.05.09 (2026-05-31)

### Admin dashboard — live time-series metrics

- **Live trend on the Metrics view.** Selecting a numeric metric now opens a
  rolling **time-series chart** (Chart.js, themed) that polls the metric on the
  configured refresh interval and keeps a 60-point window — replacing the old
  static value snapshot.
- **Value / Rate toggle.** Switch between the absolute value and a per-second
  delta (Δ/s); a downward step is shown honestly (real gauge decrease or counter
  reset) rather than smoothed away.
- **Pause/Resume** the live feed, a **Current / Min / Max / Avg** summary strip,
  and a **measurement selector** for multi-series (tagged) Prometheus metrics —
  switching reseeds the series. The measurements table refreshes live too.
- Non-numeric metrics (e.g. `python.version`) show a snapshot with a clear note
  instead of an empty chart. Rapid metric switching and navigation are race-safe
  (a load-generation token drops superseded fetches and poll ticks) and tear down
  the timer + chart on exit — no dangling intervals.
- **Responsive.** The list/detail split stacks on mobile and the chart canvas
  resizes; verified zero horizontal overflow at 390px and side-by-side at 1440px,
  in both dark and light themes.

### Admin dashboard — cache correctness

- The SPA shell (`index.html`) is now served with `Cache-Control: no-cache`, so
  the version-stamped (`?v=…`) asset URLs are actually revalidated after a
  framework upgrade. Previously a heuristically cached shell could keep pointing
  at the prior version's CSS/JS.

---

## v26.05.08 (2026-05-31)

### Admin dashboard — ⌘K command palette

- A keyboard-first **command palette**: press <kbd>⌘K</kbd> / <kbd>Ctrl-K</kbd>
  (or the new navbar **Search** button) to fuzzy-filter every view plus quick
  actions (toggle theme, wallboard mode) and jump on <kbd>Enter</kbd>.
- Full keyboard navigation (↑/↓ with clamping, Enter to run, Esc to close),
  click-to-run, and a blurred modal backdrop. Brand-green active state, Maven Pro.
- Reuses the sidebar's navigation definition (now exported) so the palette
  always stays in sync with the nav. The navbar Search trigger collapses to an
  icon on mobile.

---

## v26.05.07 (2026-05-31)

### Admin dashboard — brand refresh & UI foundation

First pass of the best-in-class admin dashboard overhaul.

- **Logo-aligned theme.** The palette is retargeted to the pyfly logo's vivid
  lime-green brand — accent/primary, sidebar, focus/active states and all chart
  palettes are green; dark surfaces shifted from navy to a desaturated
  dark-forest charcoal. Light theme accents greened to match.
- **Typography.** Switched the UI font to **Maven Pro** (rounded,
  friendly-professional — matches the logo), keeping JetBrains Mono for
  tabular/numeric data.
- **Stat cards.** Implemented the previously-empty overview stat-card icons
  (health / beans / uptime / profiles) and refined card depth (subtle gradient,
  hover lift, bolder headers).
- **Asset caching.** Admin static assets now serve with `Cache-Control: no-cache`
  and the SPA injects a `?v={__version__}` query, so theme/JS updates are picked
  up on upgrade instead of being served stale from the browser cache.
- **Responsive.** Verified zero horizontal overflow at mobile (390px) and desktop
  (1440px); mobile drawer, stacked stat cards and horizontally-scrollable tables
  all behave.
- `pyfly.__version__` kept in sync with the packaged version.

---

## v26.05.06 (2026-05-31)

### Hardening pass — framework-wide bug fixes

A deep audit of the whole framework surfaced a class of *silent wiring gaps*
and correctness bugs — features that existed but were never connected to the
runtime path, so the test suite passed while the behaviour was broken. This
release fixes them. The full test suite passes and CI (`ruff`, `ruff format`,
`mypy --strict`) is green.

#### Admin dashboard

- **HTTP traces are now recorded.** The `TraceCollectorFilter` was resolved
  from the DI container at `create_app()` time — before beans are instantiated —
  so it was always `None` and never joined the request filter chain. It is now
  created and owned by `create_app` and wired into the chain; `/admin/api/traces`
  and the SSE trace stream show real traffic.
- **Live updates (SSE) now stream.** `WebFilterChainMiddleware` buffered the
  *entire* response body before returning, which hung every infinite SSE stream
  (this broke **all** Server-Sent Events framework-wide, not just admin). The
  filter chain now forwards streaming responses incrementally.
- **Server info** resolves lazily instead of showing `unknown`.

#### Dependency injection & AOP

- **Same-type beans no longer collapse.** Two `@bean` methods returning the same
  concrete type overwrote each other in the type-keyed registry and vanished
  from `list[T]` resolution. All registrations are now tracked and
  `resolve_all`/`list[T]` returns every bean.
- **AOP advice is woven regardless of registration order.** Bean post-processing
  is now two-pass (all `before_init`, then `post_construct`+`after_init`), so a
  target initialised before its `@aspect` is still advised.
- **A side-effecting `@property` no longer aborts startup.** Weaving, scheduled-
  task discovery and every context wiring/lifecycle scan now look attributes up
  statically (`inspect.getattr_static`) instead of triggering property getters.
- **`RequestContextFilter` is wired by default**, so `REQUEST`-scoped beans and
  `@pre_authorize`/`@post_authorize` work out of the box.

#### Web

- `RequestLoggingFilter`/middleware no longer crash on every request when
  `structlog` is not installed (new `pyfly.logging.get_logger` shim).
- The FastAPI adapter now generates a correct OpenAPI document for controller
  routes and honours `@controller_advice` global exception handlers.
- The health-indicator rescan hook now actually runs after startup, so
  `/actuator/health` reflects `DOWN` subsystems instead of always reporting `UP`.
- `/actuator/prometheus` returns the Prometheus text exposition format (was JSON).

#### Transactional engine

- **Workflow `@compensation_step` now executes on failure** — completed
  compensatable steps are rolled back in reverse order.
- **Transactional REST controllers** (`/api/orchestration`, `/dlq`, `/workflow`)
  are now mounted as HTTP routes.
- The saga compensator records compensation outcomes, so
  `SagaResult.compensated`/`compensation_result` are populated.
- Saga stale-recovery no longer raises `TypeError` (`started_at` is persisted as
  a `datetime`; `get_stale` tolerates ISO strings).
- Workflow `@wait_for_all`/`@wait_for_any` timeouts are honoured (no unbounded waits).
- Fire-and-forget child workflows return the real child correlation id.

#### CQRS

- The query cache adapter now receives the `CacheAdapter`, so `@cacheable`
  queries are actually cached.
- Domain-event publishing is wired when an EDA/messaging producer bean is
  present (was a permanent no-op).

#### EDA, messaging & scheduling

- The EDA circuit breaker no longer gets permanently stuck `OPEN`.
- Kafka/RabbitMQ message-broker adapters handle `@message_listener` subscriptions
  that arrive after `start()` (they previously never consumed).
- A scheduled `fixed_delay` task that raises no longer kills its loop.

#### Data, config, event sourcing, notifications, callbacks

- Derived-query stub detection no longer misclassifies documented repository
  methods as real implementations (SQLAlchemy + MongoDB).
- MongoDB derived-query `LIKE` wildcards (`%`, `_`) are now translated to regex.
- `Config.bind()` resolves `${...}` placeholders and binds nested dataclass fields.
- `ConfigServer` filesystem backend writes back the file `fetch()` reads, so
  saves are no longer silently shadowed by a stale `.yaml`.
- The event-sourcing `ProjectionRunner` no longer advances its cursor past a
  failed event (at-least-once, in-order; was silent data loss).
- The SMTP notification provider no longer drops BCC recipients.
- Outbound callback/webhook HMAC signatures are computed over canonical JSON
  (were computed over `str(dict)` — unverifiable).

#### Internal

- `pyfly.__version__` is back in sync with the packaged version (it was stale).
- Lint/format/type fixes across the EDA adapters and correlation surface.

---

## v26.05.05 (2026-05-19)

### Fixed — `PostgresEventBus` is multi-worker safe

The Postgres EDA adapter (`pyfly.eda.adapters.postgres`) used a
per-group cursor (`pyfly_eda_offsets.last_event_id`) without any
row-level claim, so scaling consumers in the same group resulted in
**every replica dispatching every event in parallel**. The
`WHERE last_event_id < $1` guard on the cursor-advance UPDATE only
prevents going backwards — it doesn't prevent duplicate dispatch when
two replicas read the same offset concurrently.

Wrapped `_drain` in `pg_try_advisory_lock(group_key)`. The key is a
deterministic SHA-256 fold of the consumer-group name into a signed
bigint. Whoever wins the lock drains the outbox; everyone else
returns immediately and waits for the next NOTIFY or poll tick.
Session-level lock — auto-releases on connection death, so a crashed
worker never zombies the group.

The Kafka and Redis Streams adapters were already safe (their
respective brokers handle competitive consumption natively).

Helper `_group_lock_key()` exposed for tests; pinned in
[`tests/eda/test_postgres_event_bus.py::TestGroupLockKey`](tests/eda/test_postgres_event_bus.py).

---

## v26.05.04 (2026-05-08)

### Fixed — `pyfly.security` no longer needs `pyjwt` to import

`pyfly.security/__init__.py` used to eagerly re-export
`SecurityMiddleware`, which transitively imported the starlette
adapter and `pyjwt` at module load time. The chain meant that
`import pyfly` itself failed when those optional packages were
missing — even for non-HTTP services that just want the kernel + DDD
primitives.

The import is now wrapped in `try / except ImportError`, matching the
pattern already used for `JWTService` and `BcryptPasswordEncoder`.
Optional symbols (`SecurityMiddleware`, `JWTService`,
`BcryptPasswordEncoder`) only land in the `__all__` export list when
their underlying packages (`starlette`, `pyjwt`, `bcrypt`) are present.

Regression test pinned in
[`tests/security/test_optional_imports.py`](tests/security/test_optional_imports.py).

### Verified — bare-wheel install works end-to-end

* `pip install pyfly` (no extras) → `pyfly.domain` primitives
  importable, no infra deps required.
* `pip install "pyfly[web,cqrs,transactional,eventsourcing]"` →
  `PyFlyApplication`, `@enable_*_stack` decorators, and
  `register_*_stack(app)` imperative API all work without `pyjwt`
  installed.

---

## v26.05.03 (2026-05-08)

### Changed — starter decorators are now functional

The ``@enable_*_stack`` decorators (`enable_core_stack`,
`enable_application_stack`, `enable_data_stack`,
`enable_domain_stack`) used to set a marker attribute that nothing
read at boot, so the bundle they advertised did not actually take
effect. They now inject their property defaults between framework
defaults and the user's ``pyfly.yaml``:

```
framework defaults  <  starter defaults  <  user pyfly.yaml  <  profiles  <  env
```

`Config.from_sources()` accepts a new ``starter_defaults`` parameter
that ``PyFlyApplication.__init__`` populates by scanning the
application class for ``__pyfly_starter_*__`` attributes. Auto-configs
guarded on ``pyfly.X.enabled = "true"`` (CQRS, EDA, cache,
event sourcing, transactional, IDP, etc.) now wire up automatically
when the matching starter is applied.

### Added — `@enable_web_stack` (new)

Pure web-tier bundle separate from `@enable_core_stack`. Activates
``pyfly.web``, ``pyfly.server``, ``pyfly.observability``,
``pyfly.actuator`` and ``pyfly.resilience`` — useful for HTTP/REST
APIs that don't need EDA, CQRS, or cache. Java rolls these into
``starter-core``; .NET rolls them into ``Starter.Core``; pyfly keeps
them split so a non-HTTP service (worker, scheduler, CLI tool) can
opt out of the web stack entirely.

### Added — imperative `register_*_stack(app)` API

Every starter now ships an imperative counterpart for parity with
.NET's ``services.AddFireflyXxx(...)`` extension methods:

* ``register_core_stack(app)``
* ``register_web_stack(app)``
* ``register_application_stack(app)``
* ``register_data_stack(app)``
* ``register_domain_stack(app)``

Imperative registration is **authoritative** — it merges starter
properties on top of whatever's already in the config, including the
user's ``pyfly.yaml``. Mirrors .NET's last-call-wins DI semantics.

### Added — re-exports

Each starter now re-exports the most commonly used types and
decorators of its tier so a controller / service file needs only a
single import line:

* `pyfly.starters.core` re-exports `service`, `component`,
  `configuration`, `rest_controller`, `Autowired`, `Scope`,
  `pyfly_application`, `Command`, `CommandBus`, `CommandHandler`,
  `command_handler`, `Query`, `QueryBus`, `QueryHandler`,
  `query_handler`.
* `pyfly.starters.web` re-exports `rest_controller`, `controller`,
  `controller_advice`, `exception_handler`, `request_mapping`,
  `get_mapping`, `post_mapping`, `put_mapping`, `patch_mapping`,
  `delete_mapping`, `sse_mapping`, `Body`, `PathVar`, `QueryParam`,
  `Header`, `Cookie`, `File`, `UploadedFile`, `Valid`.
* `pyfly.starters.domain` re-exports the full DDD primitive set
  (`Entity`, `ValueObject`, `AggregateRoot`, `DomainEvent`,
  `Specification`, `DomainRepository`, `DomainException`,
  `BusinessRuleViolation`, `AggregateNotFound`) plus the core
  re-exports above.

### Documentation

New module guide [`docs/modules/starters.md`](docs/modules/starters.md)
explains the property-layering model, shows the cross-language
correspondence table (Java / .NET / Python), and documents both the
declarative (decorator) and imperative (function) usage patterns.

---

## v26.05.02 (2026-05-08)

### Added — `pyfly.domain` DDD building blocks

A new pure-Python module that mirrors `fireflyframework-starter-domain`
(Java) and `FireflyFramework.Starter.Domain` (.NET). Zero runtime
dependencies — just standard-library Python — so it imports from any
layer of the application.

- **`pyfly.domain.Entity[TID]`** — base class with identity-based
  equality, transient-vs-persisted distinction, and `(type, id)` hashing.
- **`pyfly.domain.ValueObject`** — marker base for `@dataclass(frozen=True)`
  records with structural equality, immutability, and a uniform
  `replace(...)` helper.
- **`pyfly.domain.AggregateRoot[TID]`** — extends `Entity[TID]` with a
  `pending_events` buffer plus `raise_event` / `pending_events` /
  `clear_events`. Distinct from the event-sourced
  `pyfly.eventsourcing.AggregateRoot`; both coexist.
- **`pyfly.domain.DomainEvent`** — frozen-dataclass base that
  auto-assigns a UUID `event_id` and UTC `occurred_at` timestamp; the
  `event_type` property defaults to the subclass name.
- **`pyfly.domain.Specification[T]`** — composable in-memory predicate
  with `&` / `|` / `~` combinators and a `Specification.of(callable)`
  factory.
- **`pyfly.domain.DomainRepository[A, TID]`** — runtime-checkable
  collection-like protocol (`add`, `find`, `remove`, `next_id`).
- **`pyfly.domain.DomainException`** + **`BusinessRuleViolation`**
  (`code="DOMAIN_RULE_VIOLATION"`) + **`AggregateNotFound`**
  (`code="DOMAIN_AGGREGATE_NOT_FOUND"`) — extend
  `pyfly.kernel.BusinessException` so existing RFC 7807 mappers,
  filters, and `@controller_advice` handlers translate them
  automatically.
- **`pyfly.starters.domain`** now re-exports every primitive above
  alongside `enable_domain_stack`, so a single import line is enough
  for a domain-tier service.

### Added — OrderService sample

`samples/order_service/` is a complete, runnable DDD microservice that
mirrors the layered split used by Java domain services in
[`firefly-oss`](https://github.com/firefly-oss) and the .NET
`FireflyFramework.Samples.OrdersService`:

```
samples/order_service/
├── interfaces/         DTOs + enums (PlaceOrderRequest, OrderDto, OrderStatus)
├── models/             Order aggregate root + repository (port + in-memory adapter)
├── core/               Commands, queries, handlers, mapper, ConfirmOrderSaga
├── web/                @rest_controller exposing /api/v1/orders
├── sdk/                Typed httpx-based client
└── app.py              @pyfly_application + @enable_domain_stack
```

`Order` is a real `AggregateRoot[str]` with state-machine invariants
enforced by `BusinessRuleViolation`. `PlaceOrderHandler` creates and
persists the aggregate and publishes its pending events.
`ConfirmOrderSaga` walks the order through
`PLACED → INVENTORY_RESERVED → PAID → SHIPPED` with full compensation
via three stub external services (`InventoryService`, `PaymentService`,
`ShippingService`). 13/13 end-to-end tests pass against the real CQRS
bus and saga engine — no mocks.

### Fixed — async saga and TCC step support

The `@saga_step`, `@try_method`, `@confirm_method`, and `@cancel_method`
decorators used to wrap the target function with a synchronous
`functools.wraps` adapter. That made `inspect.iscoroutinefunction`
return `False` for `async def` steps, so the engine called them
without `await` and the actual coroutine never ran. The wrappers were
no-ops (they just forwarded args to the original); they have been
removed and the metadata is now attached directly to the original
function. Regression test added at
`tests/transactional/saga/test_async_steps.py`.

### Documentation

- New module guide [`docs/modules/domain.md`](docs/modules/domain.md)
  with end-to-end examples for every primitive.
- README adds a "Domain — DDD Building Blocks" section to the
  Featured Patterns and a row to the Modules table; module count
  updated from 38 to 39.
- ROADMAP marks Phase 4 DDD as complete; Backoffice and Utils remain
  planned.

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
