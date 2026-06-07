<span class="eyebrow">Appendix A</span>

# Spring Boot → PyFly Cheat-Sheet {.chtitle}

If you have shipped production services with Spring Boot, the concepts in PyFly will feel immediately familiar: stereotypes, constructor injection, `@configuration` + `@bean` factories, typed config binding, derived queries, saga orchestration — they are all here. What changes is the syntax (Python decorators instead of Java annotations), the runtime model (native `async/await` instead of Project Reactor or servlet threads), and a handful of deliberate design choices made to fit idiomatic Python. This cheat-sheet maps every Spring Boot concept you already know to its PyFly equivalent, so you can start reading and writing PyFly code without relearning the architecture from scratch.

---

## Application Bootstrap & Stereotypes

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@SpringBootApplication` | `@pyfly_application` | Combines `@EnableAutoConfiguration` + `@ComponentScan`. `scan_packages` replaces classpath scanning — list packages explicitly. |
| `SpringApplication.run(...)` | `PyFlyApplication(App); await app.startup()` | Entry point is async. |
| `@Component` | `@component` | Generic singleton managed bean. |
| `@Service` | `@service` | Business-logic layer. |
| `@Repository` | `@repository` | Data-access layer. |
| `@RestController` | `@rest_controller` | API endpoint class. No `@Controller` (no view rendering). |
| `@Configuration` | `@configuration` | Bean factory class. |
| `@Bean` | `@bean` | Factory method inside a `@configuration` class. Return type hint is the bean's registered type. |
| `@Primary` | `@primary` or `@bean(primary=True)` | Default when multiple candidates exist. |
| `@Order(N)` | `@order(N)` | Lifecycle and injection ordering. |
| `@Lazy` | `@lazy` | Bean is not created until first resolution. |

---

## Dependency Injection

| Spring Boot | PyFly | Notes |
|---|---|---|
| Constructor `@Autowired` (implicit in modern Spring) | Plain constructor with type-hinted params | Container reads `__init__` type hints automatically. |
| Field `@Autowired` | `field: T = Autowired()` | `Autowired(required=False)` for optional. |
| `@Qualifier("name")` | `Annotated[T, Qualifier("name")]` | Python's `Annotated` carries the qualifier without losing the base type. |
| `Optional<T>` injection | `Optional[T]` param | Resolves to `None` when no bean is registered. |
| `List<T>` injection | `list[T]` param | Collects all registered implementations of `T`. |
| `Map<String, T>` injection | `dict[str, T]` param | `{bean-name: bean}` for every named bean of type `T`. |
| `Repository<User>` generic injection | `Repository[User, int]` param | Container matches on generic type arguments, honours `@primary` for ties. |
| `ObjectFactory<T>` / `Provider<T>` | `Provider[T]` | Deferred resolution; each `.get()` re-resolves — safe for `TRANSIENT` beans. |

!!! tip "Prefer constructor injection"
    Constructor injection keeps dependencies visible in the class signature, prevents missing-dependency bugs at startup rather than at runtime, and lets you write plain-Python unit tests without a container: `svc = WalletService(repo=MockRepo(), events=MockEvents())`.

---

## Conditions & Auto-Configuration

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@ConditionalOnProperty` | `@conditional_on_property` | Register when a config key equals a specific value. |
| `@ConditionalOnClass` | `@conditional_on_class("module")` | Register when a Python module is importable. |
| `@ConditionalOnMissingBean` | `@conditional_on_missing_bean(T)` | Register when no bean of type `T` exists yet. |
| `@ConditionalOnBean` | `@conditional_on_bean(T)` | Register only if a bean of type `T` is present. |
| `@ConditionalOnSingleCandidate` | `@conditional_on_single_candidate(T)` | Exactly one candidate, or one marked `@primary`. |
| `@ConditionalOnWebApplication` | `@conditional_on_web_application()` | Web stack (Starlette/FastAPI) present. |
| `@ConditionalOnResource` | `@conditional_on_resource(path)` | Filesystem path exists. |
| `@ConditionalOnExpression` | `@conditional_on_expression("#{...}")` | SpEL-lite expression — supports `${key:default}` + arithmetic, comparison, boolean. AST-parsed, no `eval`. |

---

## Lifecycle Hooks & Scopes

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@PostConstruct` | `@post_construct` | Called after DI; can be `async def`. |
| `@PreDestroy` | `@pre_destroy` | Called on graceful shutdown; can be `async def`. |
| Default (singleton) scope | Default (singleton) | One instance per application. |
| `@SessionScope` | `@component(scope=Scope.SESSION)` | One instance per `HttpSession`. |
| Custom `Scope` SPI | `Container.register_scope(name, handler)` | Implement the `ScopeHandler` protocol. |
| `@RefreshScope` (Spring Cloud) | `@refresh_scope` | Evicted and rebuilt on `POST /actuator/refresh`. |
| `ContextRefresher.refresh()` | `ContextRefresher.refresh()` (injectable) | Evicts refresh-scoped beans, resets `@config_properties`, returns the changed keys. |
| `ApplicationEventPublisher` | `ApplicationEventPublisher` (injectable) | `await publisher.publish(event)`. |
| `@EventListener` | `@app_event_listener` | Dispatch by `isinstance`; sync listeners are allowed. |

---

## Configuration & Profiles

| Spring Boot | PyFly | Notes |
|---|---|---|
| `application.yml` | `pyfly.yaml` | Same hierarchical structure. |
| `application-{profile}.yml` | `pyfly-{profile}.yaml` | Profile overlays. |
| `spring.profiles.active=dev` | `PYFLY_PROFILES_ACTIVE=dev` (env var) or `pyfly.profiles.active: dev` in `pyfly.yaml` | Activation is identical in priority order. |
| `@ConfigurationProperties(prefix=…)` | `@config_properties(prefix=…)` on a `@dataclass` | Pydantic-backed: validated and frozen at startup. |
| `@Value("${key}")` | `field: str = Value("${key}")` | Raises on missing key. |
| `@Value("${key:default}")` | `field: str = Value("${key:default}")` | Default after the colon. |
| `@Value("#{expr}")` SpEL | `Value("#{...}")` SpEL-lite | Arithmetic, comparison, boolean, `${...}` substitution, `env` mapping. Constructor injection: `Annotated[bool, Value("#{...}")]`. |
| `@Bean @Profile("dev")` | `@bean(profile="dev")` | Profile expression (`& \| ! ()`) on any `@bean`. |
| Boolean `@Profile("prod & cloud")` | `profile="prod & cloud"` | Spring Boot 2.4+ operators; legacy comma-OR form still works. |

Property-source priority (lowest → highest):

1. `pyfly-defaults.yaml` (framework built-ins)
2. `pyfly.yaml` (application defaults)
3. `pyfly-{profile}.yaml` (profile overlays)
4. Environment variables (runtime, highest priority)

This is identical to Spring Boot's property-source ordering.

---

## Web Layer

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@RestController` | `@rest_controller` | |
| `@RequestMapping("/path")` | `@request_mapping("/path")` | Class-level prefix. |
| `@GetMapping` | `@get_mapping` | All handler methods are `async def`. |
| `@PostMapping` | `@post_mapping` | |
| `@PutMapping` | `@put_mapping` | |
| `@DeleteMapping` | `@delete_mapping` | |
| `@PatchMapping` | `@patch_mapping` | |
| `@PathVariable Long id` | `id: int` param | Matched by name; auto type-converted. No annotation needed. |
| `@RequestParam(defaultValue="0") int page` | `page: QueryParam[int] = 0` | Python default replaces `defaultValue`. |
| `@RequestBody T body` | `body: Body[T]` | Pydantic deserialization + validation. |
| `@RequestHeader("X-Token") String t` | `t: Header[str]` | |
| `@ResponseStatus(HttpStatus.CREATED)` | `@post_mapping("/", status_code=201)` | Status code on the mapping decorator. |
| `@ControllerAdvice` + `@ExceptionHandler` | `@exception_handler` or built-in exception hierarchy | `ResourceNotFoundException` → 404, `ValidationException` → 422, etc. |
| `spring.mvc.problemdetails.enabled` | `pyfly.web.problem-details.enabled: true` | RFC 7807 `application/problem+json` responses. |

### JSON & Content Negotiation

| Spring Boot | PyFly | Notes |
|---|---|---|
| `spring.jackson.property-naming-strategy` | `pyfly.web.json.property-naming-strategy` | `as-is` (default) or `camelCase`. |
| `spring.jackson.default-property-inclusion: non_null` | `pyfly.web.json.exclude-none: true` | |
| Jackson `ObjectMapper` | `PyFlyJsonSerializer` | Central serialization boundary. |
| Jackson `Module` / custom serializer | `JsonSerializers.register(Type, encode=fn)` | Non-Pydantic type encoders. |
| `@JsonNaming(CamelCase…)` | `CamelModel` base class | Opt-in camelCase model — `order_id` serializes as `orderId`. |
| `HttpMessageConverter` | `MessageConverter` / `MessageConverterRegistry` | Built-in: JSON (first) then XML; add custom converters via `registry.add(...)`. |

---

## Data Access

| Spring Boot | PyFly | Notes |
|---|---|---|
| `JpaRepository<T, ID>` | `Repository[T, ID]` | Subclass with concrete type params; `AsyncSession` injected automatically. |
| `findByStatus(String s)` | `find_by_status(self, s: str)` | Derived-query naming convention; stub body `...` triggers generation at startup. |
| `@Query("SELECT …")` | `@query("SELECT …")` | Custom SQL on a repository method. |
| `Specification` | `Specification.where(...).and_where(...)` | Dynamic query predicates. |
| `Page<T>` / `Pageable` | `Page[T]` / `Pageable(page=0, size=20, sort="created_at:desc")` | `page.content`, `.total_elements`, `.total_pages`. |
| `@Transactional` | Unit-of-work handled by `AsyncSession` scope | Each request gets its own session via the DI container. |
| `@Transactional(readOnly = true)` | `with read_only(): session = factory()` | Routes to read replica via `RoutingSessionFactory`. |
| `AbstractRoutingDataSource` | `RoutingSessionFactory` | `factory.primary()` / `factory.replica()` to force a side. |
| Multiple `DataSource` beans | `NamedDataSources` | Config: `pyfly.data.relational.datasources.<name>`; inject `NamedDataSources`, call `.get("<name>")`. |
| Flyway migrations | `pyfly.data.migrations.*` | Same concept: versioned SQL scripts run at startup. |
| MapStruct `@Mapper` | `Mapper` + `@mapping` | Runtime reflection mapper; no codegen. `mapper.map(obj, TargetDTO)`, `mapper.map_list(...)`. |

---

## Caching

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@Cacheable(value="cache", key="#id")` | `@cacheable(backend=cache, key="item:{id}")` | `{param}` template syntax in the key. |
| `@Cacheable(condition=…, unless=…)` | `@cacheable(condition=…, unless=…)` | `condition` bypasses on args; `unless` skips storing based on result. |
| `@CacheEvict` | `@cache_evict(backend=cache, key="item:{id}")` | |
| `@CachePut` | `@cache_put(backend=cache, key="item:{id}")` | Update cache after mutation. |
| `CacheManager` (auto-configured) | `InMemoryCache` / `RedisCacheAdapter` (auto-configured) | Redis selected when the `redis` extra is installed; falls back to in-memory. |

---

## Messaging & Events

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@KafkaListener(topics=…, groupId=…)` | `@message_listener(topic=…, group_id=…)` | Handler is `async def`. |
| `KafkaTemplate.send(topic, event)` | `await event_bus.publish(EventEnvelope(...))` | `MessageBrokerPort` abstraction; swap Kafka for RabbitMQ without changing callers. |
| `@RetryableTopic` / DLT | `@message_listener(retries=3, retry_delay=1.0, dead_letter_topic="…")` | Linear-backoff retry; exhausted messages routed to DLQ with `x-original-topic` / `x-exception` headers. |
| `ApplicationEvent` | `EventEnvelope` | Domain event container. |
| `@EventListener` | `@event_listener` | In-process event handler. |
| `ApplicationEventPublisher` | `ApplicationEventPublisher` (injectable) | `await publisher.publish(event)`. |

!!! tip "Messaging vs EDA"
    PyFly separates **broker messaging** (`pyfly.messaging` — Kafka/RabbitMQ transport) from **domain events** (`pyfly.eda` — `EventEnvelope` + `EventBus`). Start with `InMemoryEventBus` inside a monolith; switch to a Kafka adapter later by changing one configuration key, not your handlers.

---

## Security

| Spring Boot | PyFly | Notes |
|---|---|---|
| `SecurityAutoConfiguration` | `JwtAutoConfiguration` + `PasswordEncoderAutoConfiguration` | Split by optional dependency. |
| JWT filter chain | `JwtAutoConfiguration` auto-wired | Enable with `pyfly.security.jwt.enabled: true`. |
| `@PreAuthorize("hasRole('ADMIN')")` | `@pre_authorize("hasRole('ADMIN')")` | Same SpEL subset: `hasRole`, `hasAnyRole`, `hasAuthority`, `isAuthenticated`, `permitAll`, `denyAll`, `#param`, `and`/`or`/`not`. AST-walked, no `eval`. |
| `@PostAuthorize("returnObject.owner == principal")` | `@post_authorize("returnObject.owner == principal")` | `returnObject` is bound to the method's return value. |
| `RoleHierarchy` bean | `RoleHierarchy.from_string("ADMIN > USER")` + `set_role_hierarchy(...)` | `expand(roles)` consulted by `hasRole`/`hasAnyRole`. |
| `ClientRegistration` (OAuth2 auth-code) | `ClientRegistration(...)` | PKCE: add `use_pkce=True` — generates `code_verifier`/`code_challenge` (S256) automatically. |
| `maximumSessions` / `SessionRegistry` | `SessionConcurrencyController` + `SessionRegistry` | Cap per-principal concurrent sessions (`evict-oldest` or `reject-new`). Enable: `pyfly.session.concurrency.enabled: true`. |

---

## Scheduling

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@Scheduled(fixedRate = 5000)` | `@scheduled(fixed_rate=5.0)` | Spring uses milliseconds; PyFly uses **seconds**. |
| `@Scheduled(fixedDelay = 1000)` | `@scheduled(fixed_delay=1.0)` | |
| `@Scheduled(cron = "0 0 2 * * ?")` | `@scheduled(cron="0 2 * * *")` | Spring: 6-field (seconds-first). PyFly: standard 5-field; also accepts the 6-field Spring form and `?`. |
| `@Scheduled(cron=…, zone=…)` | `@scheduled(cron=…, zone="America/New_York")` | IANA time zone; ignored for `fixed_rate`/`fixed_delay`. |
| ShedLock / `@SchedulerLock` | `@scheduled(lock=True, lock_ttl=30)` + `DistributedLock` bean | Skips a tick when the lock is held elsewhere. Defaults to in-process `LocalLock`; register a Redis `DistributedLock` for cross-process single-firing. |
| `@EnableScheduling` | `SchedulingAutoConfiguration` | Auto-enabled when `croniter` is installed. No explicit `@Enable…` needed. |

---

## Resilience

| Spring (Resilience4j) | PyFly | Notes |
|---|---|---|
| `@Retry` | `@retry(max_attempts=3, delay=0.1, backoff=2.0, jitter=True, exceptions=[IOError])` | Sync + async; exponential backoff with optional cap. |
| `@CircuitBreaker` | `CircuitBreaker(...) + @circuit_breaker(breaker)` | Count-based (`failure_threshold`) or rate-based (`failure_rate_threshold` + `window_size`); `half_open_max_calls` trial calls. |
| `@RateLimiter` | `RateLimiter(max_tokens=100, refill_rate=100/60) + @rate_limiter(limiter)` | Token-bucket. |
| `@Bulkhead` | `Bulkhead(max_concurrent=10) + @bulkhead(bh)` | Concurrency cap. |
| `@TimeLimiter` | `@time_limiter(timeout=timedelta(seconds=2))` | Raises `asyncio.TimeoutError` on breach. |
| `fallbackMethod` | `@fallback(fallback_method=fn)` or `@fallback(fallback_value=v)` | Static or callable fallback. |

---

## AOP

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@Aspect` + `@Component` | `@aspect` + `@component` | |
| `@Before("execution(…)")` | `@before("execution(…)")` | |
| `@After` | `@after` | Always runs (like `finally`). |
| `@Around` | `@around` | Call `await join_point.proceed()` to continue. |
| `@AfterReturning` | `@after_returning` | |
| `@AfterThrowing` | `@after_throwing` | |
| `@EnableAspectJAutoProxy` | `AopAutoConfiguration` | Always active; no opt-in needed. |

Pointcut DSL: `execution(* pkg.services.*.*(..))` for method patterns; `annotation(timed)` for decorator-targeted matching.

---

## Observability & Actuator

| Spring Boot | PyFly | Notes |
|---|---|---|
| `management.endpoints.web.exposure.include` | `pyfly.management.endpoints.web.exposure.include` | |
| `/actuator/health` | `/actuator/health` | |
| `/actuator/info` | `/actuator/info` | |
| `/actuator/beans` | `/actuator/beans` | |
| `/actuator/env` | `/actuator/env` | |
| `POST /actuator/refresh` (Spring Cloud) | `POST /actuator/refresh` | Evicts refresh-scoped beans; resets `@config_properties`; returns the changed keys. |
| Micrometer `@Timed` | `@timed("metric_name")` | Method timing histogram. |
| Micrometer `@Counted` | `@counted("metric_name")` | Invocation counter. |
| Prometheus `MetricsRegistry` | `MetricsRegistry` (Prometheus backend) | Auto-configured when `prometheus_client` is installed. |
| Sleuth / Micrometer Tracing (W3C) | `TracingFilter` (inbound) + `HttpxClientAdapter` (outbound) | W3C `traceparent` extracted into a SERVER span; injected on outbound httpx calls. `trace_id`/`span_id` stamped in logs via `StructlogAdapter`. Safe no-op without OpenTelemetry. |

---

## CQRS & Sagas

| Spring Boot / Axon | PyFly | Notes |
|---|---|---|
| `@CommandHandler` | `@command_handler` | Dispatched by `CommandBus`. |
| `@QueryHandler` | `@query_handler` | Dispatched by `QueryBus`. |
| `@EventHandler` (event-sourcing) | `@event_handler` | Sourced from `EventStore`. |
| `@Saga` | `@saga(name="…", layer_concurrency=N)` | Saga orchestration class. |
| `@SagaStep(id=…, compensate=…)` | `@saga_step(id="…", compensate="method_name", retry=3, backoff_ms=100, timeout_ms=5000)` | |
| `@Input` | `Annotated[T, Input]` | Inject the saga's initial payload. |
| `@FromStep("id")` | `Annotated[T, FromStep("id")]` | Inject the output of a prior step. |
| `@Tcc` | `@tcc(name="…")` | TCC (Try-Confirm-Cancel) transaction class. |
| `@TccParticipant` | `@tcc_participant(id="…", order=N)` | |
| `@TryMethod` / `@ConfirmMethod` / `@CancelMethod` | `@try_method` / `@confirm_method` / `@cancel_method` | TCC three-phase methods. |
| `@FromTry` | `Annotated[T, FromTry]` | Inject the try-phase result into confirm/cancel. |

---

## Integration Testing

| Spring Boot | PyFly | Notes |
|---|---|---|
| `@SpringBootTest` | `service_slice(*beans)` / `slice_context(...)` | Minimal started context; `overrides` accept a class or pre-built instance. |
| `@WebMvcTest` | `web_slice(*controllers, overrides=…)` → `(context, client)` | Starts minimal context + `PyFlyTestClient`. |
| `@DataJpaTest` | `data_slice(*beans)` → `context` | Data-layer slice. |
| `@Testcontainers` + `@Container` | `with postgres_container() as pg:` | Python context manager handles lifecycle. |
| `@ServiceConnection` | `pyfly_config(pg)` / `pyfly_config_for(pg)` | Maps container connection details into PyFly config keys. |
| `@DynamicPropertySource` | `pyfly_config(*containers, base=…)` | One-call `Config` for several containers. |
| `PostgreSQLContainer` | `postgres_container()` | URL auto-rewritten to `asyncpg`. |
| `MySQLContainer` | `mysql_container()` | URL rewritten to `aiomysql`. |
| `GenericContainer` (Redis) | `redis_container()` | Cache + session URLs wired. |
| `KafkaContainer` | `kafka_container()` | |
| `@requires_docker` | `@requires_docker` | Skips test cleanly when Docker daemon is absent. |

Install with: `pip install 'pyfly[testcontainers]'`

---

## Embedded Server

| Spring Boot | PyFly | Notes |
|---|---|---|
| `server.port` | `pyfly.web.port` | HTTP listen port. |
| Tomcat (default) | Granian (default) | Rust/tokio HTTP runtime; highest priority. |
| Jetty (fallback) | Uvicorn (fallback) | Ecosystem-standard ASGI fallback. |
| Undertow (alternative) | Hypercorn (alternative) | Advanced protocol support (HTTP/3). |
| `server.tomcat.*` | `pyfly.server.granian.*` | Server-specific tuning. |
| `WebServer` interface | `ApplicationServerPort` protocol | Contract for the embedded ASGI server. |
| `EventLoopGroup` (Netty) | `EventLoopPort` protocol | Contract for the I/O runtime. |
| `server.type: auto` | `pyfly.server.type: auto` (default) | Granian → Uvicorn → Hypercorn cascade. |

!!! tip "Auto-configuration cascade"
    PyFly's auto-configuration uses the same conditional-bean pattern as Spring Boot: if Granian is installed, `GranianServerAdapter` wins; if not, Uvicorn is tried next; then Hypercorn. Override at any point by providing your own `ApplicationServerPort` bean.
