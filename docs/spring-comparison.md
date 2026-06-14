# Spring Boot Comparison Guide

**A comprehensive mapping from Spring Boot to PyFly for Java developers.**

If you're coming from the Java/Spring Boot ecosystem, this guide shows you how every concept you know translates into PyFly. Each section explains not just *what* maps to *what*, but *why* PyFly chose its approach and how the Python-native design differs from the Java original.

---

## Table of Contents

- [Application Entry Point](#application-entry-point)
- [Dependency Injection](#dependency-injection)
- [Bean Stereotypes](#bean-stereotypes)
- [Bean Configuration](#bean-configuration)
- [Conditional Beans](#conditional-beans)
- [Lifecycle Hooks](#lifecycle-hooks)
- [Configuration Properties](#configuration-properties)
- [Profiles](#profiles)
- [Web Controllers](#web-controllers)
- [Request Parameters](#request-parameters)
- [Exception Handling](#exception-handling)
- [JSON Serialization and Content Negotiation](#json-serialization-and-content-negotiation)
- [Data Access](#data-access)
- [Caching](#caching)
- [Scheduling](#scheduling)
- [Aspect-Oriented Programming](#aspect-oriented-programming)
- [Resilience Patterns](#resilience-patterns)
- [Observability](#observability)
- [Messaging](#messaging)
- [Distributed Transactions](#distributed-transactions)
- [Server Abstraction](#server-abstraction)
- [Integration Testing with Containers](#integration-testing-with-containers)
- [More Spring-parity features (v26.06.37–55)](#more-spring-parity-features-v260637-55)
- [Quick Reference Table](#quick-reference-table)

---

## Application Entry Point

### Spring Boot

```java
@SpringBootApplication
public class MyApplication {
    public static void main(String[] args) {
        SpringApplication.run(MyApplication.class, args);
    }
}
```

`@SpringBootApplication` is a convenience annotation that combines `@Configuration`, `@EnableAutoConfiguration`, and `@ComponentScan`.

### PyFly

```python
from pyfly.core import pyfly_application, PyFlyApplication

@pyfly_application(
    name="my-service",
    version="0.1.0",
    scan_packages=["my_service"],
)
class Application:
    pass

# Start the application
app = PyFlyApplication(Application)
await app.startup()
```

**Key difference:** In Spring, component scanning is classpath-based and implicit. In PyFly, `scan_packages` explicitly lists which Python packages to scan for decorated classes. This is deliberate — Python's import system doesn't have Java's classpath scanning, so explicit package listing is more predictable and avoids accidental imports from third-party libraries.

**Configuration:** Spring uses `application.yml` or `application.properties`. PyFly uses `pyfly.yaml` with the same hierarchical structure. See [Configuration Guide](modules/configuration.md).

---

## Dependency Injection

### Spring Boot

```java
@Service
public class OrderService {
    private final OrderRepository repo;
    private final EventPublisher events;

    @Autowired  // Optional in modern Spring with single constructor
    public OrderService(OrderRepository repo, EventPublisher events) {
        this.repo = repo;
        this.events = events;
    }
}
```

### PyFly — Constructor Injection (Preferred)

```python
@service
class OrderService:
    def __init__(self, repo: OrderRepository, events: EventPublisher) -> None:
        self._repo = repo
        self._events = events
```

### PyFly — Field Injection with `Autowired()`

```python
from pyfly.container import Autowired

@service
class OrderService:
    repo: OrderRepository = Autowired()
    events: EventPublisher = Autowired()
    metrics: MetricsCollector = Autowired(required=False)  # optional
```

**How it works:** PyFly supports both constructor injection and field injection, matching Spring Boot's capabilities. Constructor injection is the recommended default — it makes dependencies explicit and enables immutability. Field injection via `Autowired()` is available for cases where it improves readability or for optional dependencies.

The container inspects `__init__` type hints for constructor injection and class annotations for `Autowired()` sentinels. After constructing the instance, it injects any `Autowired` fields via `setattr`.

### Optional and Collection Injection

```python
from typing import Optional

@service
class OrderService:
    def __init__(
        self,
        repo: OrderRepository,
        cache: Optional[CacheAdapter] = None,    # None if not registered
        validators: list[Validator] = [],          # all implementations
    ) -> None:
        self._repo = repo
        self._cache = cache
        self._validators = validators
```

`Optional[T]` resolves to `None` when no bean of type `T` is registered. `list[T]` collects all implementations bound to `T` — equivalent to Spring's `List<T>` injection.

### Qualifier / Named Beans

**Spring:**
```java
@Autowired
public OrderService(@Qualifier("postgresRepo") OrderRepository repo) { }
```

**PyFly:**
```python
from typing import Annotated
from pyfly.container import Qualifier

@service
class OrderService:
    def __init__(self, repo: Annotated[OrderRepository, Qualifier("postgres_repo")]) -> None:
        self._repo = repo
```

PyFly uses Python's `Annotated` type hint with `Qualifier` metadata instead of a separate annotation. This keeps the type system clean — the base type is still `OrderRepository` for type checking, while `Qualifier` provides the additional lookup hint.

### Primary Bean

**Spring:**
```java
@Primary
@Repository
public class PostgresOrderRepo implements OrderRepository { }
```

**PyFly:**
```python
@primary
@repository
class PostgresOrderRepo:
    """This implementation is used when multiple OrderRepository beans exist."""
    pass
```

When multiple beans satisfy the same type, `@primary` marks the default.

### Injecting Configuration Values — `@Value`

**Spring:**
```java
@Service
public class MailService {
    @Value("${mail.host}")
    private String host;

    @Value("${mail.port:25}")
    private int port;

    public MailService(@Value("#{${mail.workers:1} > 1}") boolean concurrent) { }
}
```

**PyFly:**
```python
from typing import Annotated
from pyfly.core import Value

@service
class MailService:
    # Field injection — resolved against Config at bean creation time
    host: str = Value("${mail.host}")           # raises if missing
    port: int = Value("${mail.port:25}")        # default after the colon

    # Constructor injection — wrap with Annotated, the value is coerced to the param type
    def __init__(self, concurrent: Annotated[bool, Value("#{${mail.workers:1} > 1}")]) -> None:
        self._concurrent = concurrent
```

`Value` lives in `pyfly.core`. It supports three expression forms:

| Form | Behavior |
|------|----------|
| `${key}` | Resolve from `Config`; raise if missing |
| `${key:default}` | Resolve from `Config`, use `default` if missing |
| `#{ ... }` | Evaluate a SpEL-lite expression (the pyfly subset of Spring's SpEL) |

The `#{ ... }` evaluator supports arithmetic, comparison, boolean (`and`/`or`/`not`), the Python ternary (`a if c else b`), literals, lists/tuples, `${key:default}` placeholder substitution, and an `env` mapping for environment variables. It is parsed with `ast` against a whitelist of node types — there is no `eval`, no attribute access, and no function calls, so an expression can never execute arbitrary code. This is intentionally narrower than Spring's full SpEL.

### Deferred Resolution — `Provider[T]` (Spring `ObjectFactory`/`Provider`)

**Spring:**
```java
@Service
public class Worker {
    private final ObjectFactory<Job> jobs;
    public Worker(ObjectFactory<Job> jobs) { this.jobs = jobs; }
    public void run() { Job job = jobs.getObject(); }
}
```

**PyFly:**
```python
from pyfly.container import Provider

@service
class Worker:
    def __init__(self, jobs: Provider[Job]) -> None:
        self._jobs = jobs

    def run(self) -> None:
        job = self._jobs.get()   # or self._jobs() — fresh resolution each call
```

Inject `Provider[T]` instead of `T` to defer resolution. Each `.get()` (or calling the provider directly) re-resolves the bean — so a singleton can obtain fresh `TRANSIENT` instances, and construction-time cycles or expensive beans can be deferred until first use. This is the Spring `ObjectFactory`/`Provider` equivalent.

### Map Injection (Spring `Map<String, T>`)

**Spring:**
```java
public PaymentRouter(Map<String, PaymentGateway> gateways) { }
```

**PyFly:**
```python
@service
class PaymentRouter:
    def __init__(self, gateways: dict[str, PaymentGateway]) -> None:
        self._gateways = gateways   # {bean-name: bean} for every named PaymentGateway
```

Declare a `dict[str, T]` parameter and PyFly injects a map of `{bean-name: bean}` for every named bean assignable to `T` — exactly like Spring's `Map<String, T>` injection.

### Generic Repository Injection (generic-aware DI)

**Spring** resolves `Repository<User>` to the implementation parametrized with `User`. **PyFly** does the same:

```python
@service
class UserService:
    def __init__(self, repo: Repository[User, int]) -> None:
        self._repo = repo   # the registered Repository subclass parametrized with User
```

When several implementations share a generic interface, the container matches the one whose generic bases carry the requested type args (honoring `@primary` to break ties), mirroring Spring's generic-aware injection.

### Lazy Beans — `@lazy` (Spring `@Lazy`)

**Spring:**
```java
@Lazy
@Service
public class ExpensiveService { }
```

**PyFly:**
```python
from pyfly.container import lazy

@lazy
@service
class ExpensiveService:
    """Not created at startup — constructed on first resolution instead."""
```

A `@lazy` bean is **not** eagerly created during startup; it is constructed on first resolution. Useful for expensive beans that may never be used, or to avoid heavy work at boot — the Spring `@Lazy` equivalent.

---

## Bean Stereotypes

Spring and PyFly share the same stereotype hierarchy, but the semantics are slightly different:

| Spring | PyFly | Scope | Purpose |
|--------|-------|-------|---------|
| `@Component` | `@component` | Singleton | Generic managed bean |
| `@Service` | `@service` | Singleton | Business logic |
| `@Repository` | `@repository` | Singleton | Data access |
| `@Controller` | `@rest_controller` | Singleton | HTTP endpoints |
| `@Configuration` | `@configuration` | Singleton | Bean factory class |

**Why PyFly uses `@rest_controller` instead of `@controller`:** In Spring, `@Controller` renders views and `@RestController` returns JSON. Since PyFly is API-first and doesn't have a templating engine, `@rest_controller` is the standard stereotype. It automatically serializes return values to JSON.

### Stereotype Behavior

All stereotypes in both frameworks:
- Mark the class as a **managed bean** (created and owned by the container)
- Default to **singleton scope** (one instance per application)
- Enable **component scanning** (auto-discovered at startup)
- Support **constructor injection** (dependencies resolved automatically)

---

## Bean Configuration

### Spring Boot

```java
@Configuration
public class DatabaseConfig {
    @Bean
    public DataSource dataSource() {
        return new HikariDataSource(hikariConfig());
    }

    @Bean
    @ConditionalOnProperty(name = "cache.enabled", havingValue = "true")
    public CacheManager cacheManager() {
        return new RedisCacheManager();
    }
}
```

### PyFly

```python
@configuration
class DatabaseConfig:
    @bean
    def data_source(self) -> DataSource:
        return HikariDataSource(self._hikari_config())

    @bean
    @conditional_on_property("cache.enabled", having_value="true")
    def cache_manager(self) -> CacheManager:
        return RedisCacheManager()
```

**Key difference:** Spring's `@Bean` methods are processed by CGLIB proxying to ensure singleton behavior — calling `dataSource()` twice returns the same instance. PyFly doesn't need this because `@bean` methods are only called once during container initialization; the container manages the singleton lifecycle directly.

**Return type hint:** PyFly uses the return type annotation (`-> DataSource`) to determine what type this bean satisfies. This is equivalent to Spring inferring the bean type from the method return type.

### Primary and Profile-Scoped `@bean`

`@bean` accepts the same modifiers Spring expresses with `@Primary` and `@Profile`:

**Spring:**
```java
@Bean
@Primary
public PaymentGateway stripeGateway() { return new StripeGateway(); }

@Bean
@Profile("dev")
public PaymentGateway sandboxGateway() { return new SandboxGateway(); }
```

**PyFly:**
```python
@configuration
class PaymentConfig:
    @bean(primary=True)
    def stripe_gateway(self) -> PaymentGateway:
        return StripeGateway()

    @bean(profile="dev")
    def sandbox_gateway(self) -> PaymentGateway:
        return SandboxGateway()
```

`primary=True` marks the bean the default candidate when several share an interface (the `@Bean @Primary` equivalent). `profile="dev"` creates the bean only when the expression matches the active profiles (the `@Bean @Profile` equivalent). `@bean` also takes `name=` and `scope=`.

---

## Conditional Beans

Both frameworks support conditional bean registration based on runtime conditions:

| Spring | PyFly | Purpose |
|--------|-------|---------|
| `@ConditionalOnProperty` | `@conditional_on_property` | Register only if config key has specific value |
| `@ConditionalOnClass` | `@conditional_on_class` | Register only if a Python module is importable |
| `@ConditionalOnMissingBean` | `@conditional_on_missing_bean` | Register only if no bean of that type exists |
| `@ConditionalOnBean` | `@conditional_on_bean` | Register only if a bean of that type exists |
| `@ConditionalOnExpression` | `@conditional_on_expression` | Register only if a SpEL-lite `#{ ... }` expression is truthy |

### Example: Expression-Based Conditions

`@conditional_on_expression` evaluates the same SpEL-lite `#{ ... }` syntax used by `@Value`, against the active config at `ApplicationContext` startup:

```python
from pyfly.context import conditional_on_expression

@configuration
@conditional_on_expression("#{${pyfly.workers:1} > 1}")
class MultiWorkerConfig:
    """Only registered when more than one worker is configured."""
```

`${key:default}` placeholders are substituted from config before evaluation, and an `env` mapping exposes environment variables — mirroring Spring Boot's `@ConditionalOnExpression`.

### Example: Auto-Configuration

```python
@configuration
class CacheAutoConfiguration:
    @bean
    @conditional_on_class("redis")
    def redis_cache(self) -> CacheAdapter:
        return RedisCacheAdapter()

    @bean
    @conditional_on_missing_bean(CacheAdapter)
    def in_memory_cache(self) -> CacheAdapter:
        return InMemoryCache()
```

This mirrors Spring Boot's auto-configuration pattern exactly: if Redis is installed, use it; otherwise, fall back to an in-memory implementation. Your application code depends on `CacheAdapter` (the port) and never knows which implementation is active.

---

## Lifecycle Hooks

### Spring Boot

```java
@Component
public class DataLoader {
    @PostConstruct
    public void init() {
        // Called after dependency injection
    }

    @PreDestroy
    public void cleanup() {
        // Called during shutdown
    }
}
```

### PyFly

```python
@component
class DataLoader:
    @post_construct
    async def init(self) -> None:
        # Called after dependency injection
        await self._load_initial_data()

    @pre_destroy
    async def cleanup(self) -> None:
        # Called during graceful shutdown
        await self._flush_buffers()
```

**Key difference:** PyFly lifecycle hooks are `async` — they can perform I/O operations like loading data from a database or flushing to a message broker. Spring's `@PostConstruct` is synchronous by design.

**Ordering:** Use `@order(N)` to control lifecycle hook execution order across beans, just like Spring's `@Order`.

---

## Configuration Properties

### Spring Boot

```java
@ConfigurationProperties(prefix = "app.datasource")
public class DataSourceProperties {
    private String url;
    private int poolSize = 10;
    // getters, setters
}
```

```yaml
app:
  datasource:
    url: jdbc:postgresql://localhost/mydb
    pool-size: 20
```

### PyFly

```python
from dataclasses import dataclass
from pyfly.core import config_properties

@config_properties(prefix="pyfly.data")
@dataclass
class DataSourceProperties:
    url: str = "sqlite+aiosqlite:///app.db"
    pool_size: int = 10
```

```yaml
pyfly:
  data:
    url: postgresql+asyncpg://localhost/mydb
    pool_size: 20
```

**Key difference:** Spring uses Java beans with getters/setters. PyFly uses Pydantic `BaseModel` classes, which gives you:
- **Automatic validation** — Invalid config values are caught at startup with clear error messages
- **Type coercion** — String environment variables are automatically converted to the right type
- **Immutability** — Config objects are frozen after creation (Pydantic `frozen=True`)
- **Default values** — Python default arguments are cleaner than Java's field initialization

---

## Profiles

### Spring Boot

```yaml
# application-dev.yml
spring:
  datasource:
    url: jdbc:h2:mem:testdb

# application-prod.yml
spring:
  datasource:
    url: jdbc:postgresql://prod-db/mydb
```

Activated via: `spring.profiles.active=dev`

### PyFly

```yaml
# pyfly-dev.yaml
pyfly:
  data:
    url: sqlite+aiosqlite:///dev.db
  logging:
    level:
      root: DEBUG

# pyfly-prod.yaml
pyfly:
  data:
    url: postgresql+asyncpg://prod-db/mydb
  logging:
    level:
      root: WARNING
```

Activated via: `PYFLY_PROFILES_ACTIVE=dev` environment variable, or in `pyfly.yaml`:

```yaml
pyfly:
  profiles:
    active: dev
```

**Configuration layering** (lowest to highest priority):
1. `pyfly-defaults.yaml` — Framework built-in defaults
2. `pyfly.yaml` — Your application defaults
3. `pyfly-{profile}.yaml` — Profile-specific overrides
4. Environment variables — Runtime overrides (highest priority)

This is identical to Spring Boot's property source ordering.

---

## Web Controllers

### Spring Boot

```java
@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private final OrderService service;

    public OrderController(OrderService service) {
        this.service = service;
    }

    @GetMapping
    public List<Order> listOrders() {
        return service.findAll();
    }

    @GetMapping("/{id}")
    public Order getOrder(@PathVariable Long id) {
        return service.findById(id);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public Order createOrder(@RequestBody CreateOrderRequest request) {
        return service.create(request);
    }
}
```

### PyFly

```python
@rest_controller
@request_mapping("/api/orders")
class OrderController:
    def __init__(self, service: OrderService) -> None:
        self._service = service

    @get_mapping("/")
    async def list_orders(self) -> list[dict]:
        return await self._service.find_all()

    @get_mapping("/{id}")
    async def get_order(self, id: int) -> dict:
        return await self._service.find_by_id(id)

    @post_mapping("/", status_code=201)
    async def create_order(self, request: Body[CreateOrderRequest]) -> dict:
        return await self._service.create(request)
```

**Key differences:**
- **Async handlers:** All PyFly handlers are `async` — they run on the asyncio event loop, not a thread pool
- **Path parameters:** In PyFly, path parameters like `{id}` are automatically resolved from method parameter names and type-converted. No `@PathVariable` annotation needed — just matching parameter names
- **Request body:** `Body[T]` is a type alias that tells PyFly to deserialize the request body into a Pydantic model. Spring's `@RequestBody` is an annotation on the parameter
- **Response:** PyFly automatically serializes the return value to JSON. Return `dict`, Pydantic models, or dataclasses

### HTTP Method Mappings

| Spring | PyFly |
|--------|-------|
| `@GetMapping("/path")` | `@get_mapping("/path")` |
| `@PostMapping("/path")` | `@post_mapping("/path")` |
| `@PutMapping("/path")` | `@put_mapping("/path")` |
| `@DeleteMapping("/path")` | `@delete_mapping("/path")` |
| `@PatchMapping("/path")` | `@patch_mapping("/path")` |

---

## Request Parameters

| Spring | PyFly | Description |
|--------|-------|-------------|
| `@PathVariable Long id` | `id: int` | URL path parameter (matched by name) |
| `@RequestParam String name` | `name: QueryParam[str]` | Query string parameter |
| `@RequestBody Order order` | `order: Body[Order]` | JSON request body |
| `@RequestHeader("X-Token") String token` | `token: Header[str]` | HTTP header value |

**Spring example:**
```java
@GetMapping("/search")
public List<Order> search(
    @RequestParam String status,
    @RequestParam(defaultValue = "0") int page,
    @RequestParam(defaultValue = "20") int size
) {
    return service.search(status, page, size);
}
```

**PyFly equivalent:**
```python
@get_mapping("/search")
async def search(
    self,
    status: QueryParam[str],
    page: QueryParam[int] = 0,
    size: QueryParam[int] = 20,
) -> list[dict]:
    return await self._service.search(status, page, size)
```

Python default arguments replace Spring's `defaultValue`.

---

## Exception Handling

### Spring Boot

```java
@ControllerAdvice
public class GlobalExceptionHandler {
    @ExceptionHandler(ResourceNotFoundException.class)
    public ResponseEntity<ErrorResponse> handleNotFound(ResourceNotFoundException ex) {
        return ResponseEntity.status(404).body(new ErrorResponse(ex.getMessage()));
    }
}
```

### PyFly

PyFly provides automatic exception-to-HTTP mapping through its exception hierarchy. Each exception class has a pre-defined HTTP status code:

```python
from pyfly.kernel import ResourceNotFoundException, ValidationException

# Automatically returns 404
raise ResourceNotFoundException("Order not found", code="ORDER_NOT_FOUND")

# Automatically returns 422
raise ValidationException("Invalid order data", code="VALIDATION_ERROR")
```

| PyFly Exception | HTTP Status |
|----------------|-------------|
| `ValidationException` | 422 |
| `ResourceNotFoundException` | 404 |
| `ConflictException` | 409 |
| `UnauthorizedException` | 401 |
| `ForbiddenException` | 403 |
| `RateLimitException` | 429 |
| `ServiceUnavailableException` | 503 |

**Key difference:** Spring requires you to write `@ControllerAdvice` classes to map exceptions to responses. PyFly's exception hierarchy has this mapping built in — just throw the right exception and the framework produces a structured error response automatically.

You can still add custom exception handlers per controller using `@exception_handler` for cases where you need custom response formatting.

### RFC 7807 `application/problem+json`

By default PyFly emits an `{"error": {...}}` envelope. Like Spring Boot 3, it can instead emit RFC 7807 `application/problem+json` — opt in with one config key:

```yaml
pyfly:
  web:
    problem-details:
      enabled: true
```

The response then uses the standard `type`, `title`, `status`, `detail`, and `instance` members, plus pyfly extension members (`code`, `transactionId`, `timestamp`, and `context` when present), served with the `application/problem+json` media type:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "Order not found",
  "instance": "/api/orders/42",
  "code": "ORDER_NOT_FOUND",
  "transactionId": "…",
  "timestamp": "…"
}
```

---

## JSON Serialization and Content Negotiation

### Jackson `ObjectMapper` → `pyfly.web.json`

In Spring, a central `ObjectMapper` (configured globally via `spring.jackson.*`) serializes every response. PyFly keeps **per-model** behavior with Pydantic (`Field(alias=...)`, `@field_serializer`, discriminated unions) and adds the piece Spring centralizes: **global** JSON config applied at one serialization boundary, plus a registry for non-Pydantic types.

**Spring:**
```yaml
spring:
  jackson:
    property-naming-strategy: LOWER_CAMEL_CASE
    default-property-inclusion: non_null
```

**PyFly:**
```yaml
pyfly:
  web:
    json:
      property-naming-strategy: as-is   # as-is | camelCase (camelCase implies by-alias on output)
      by-alias: false
      exclude-none: false
      exclude-defaults: false
      fail-on-unknown-properties: false
```

These keys bind to `JsonProperties` and are applied by `PyFlyJsonSerializer` at the response boundary.

**Custom encoders for non-Pydantic types** (the Jackson module/serializer equivalent) go through `JsonSerializers`:

```python
from pyfly.web.json import JsonSerializers

serializers = JsonSerializers()
serializers.register(Money, encode=lambda m: {"amount": str(m.amount), "ccy": m.currency})
```

**Opt-in camelCase models** use the `CamelModel` base instead of a global alias generator that mutates your models:

```python
from pyfly.web.json import CamelModel

class OrderResponse(CamelModel):
    order_id: int      # serializes as "orderId"; also accepts snake_case input
    total_amount: float
```

`pyfly.web.json` is deliberately **not** a Jackson clone — there is no `@JsonView`, no `ObjectMapper` god-object, no codegen, and no global alias generator injected into your models.

### `HttpMessageConverter` → `message_converters`

Spring's `HttpMessageConverter` registry reads request bodies and writes responses based on content negotiation. PyFly mirrors this with an ordered, pluggable `MessageConverterRegistry`:

```python
from pyfly.web.message_converters import (
    default_message_converters,
    MessageConverter,
    MessageConverterRegistry,
)

# Built-in registry: JSON (first/default) then XML, sharing one serializer
registry = default_message_converters()

# Register your own (highest priority) — e.g. a CBOR converter
class CborConverter(MessageConverter):
    media_types = ("application/cbor",)
    def read(self, body: bytes, target_type: type): ...
    def write(self, value): ...

registry.add(CborConverter())
```

| Spring | PyFly | Behavior |
|--------|-------|----------|
| `HttpMessageConverter` | `MessageConverter` | Reads/writes bodies for its media types |
| `MappingJackson2HttpMessageConverter` | `JsonMessageConverter` | JSON via `PyFlyJsonSerializer` + Pydantic validation |
| `MappingJackson2XmlHttpMessageConverter` | `XmlMessageConverter` | XML via stdlib `ElementTree` |
| Converter list on `WebMvcConfigurer` | `MessageConverterRegistry` | Ordered; first match wins |

Reads select a converter by the request `Content-Type`; writes select by the `Accept` header, ordered by **q-value** (`parse_accept` honors `q=` weights). All formats route JSON-level serialization through `PyFlyJsonSerializer`, so the global `pyfly.web.json.*` config applies to every format. With `fail-on-unknown-properties` enabled, the JSON converter validates request bodies against an `extra='forbid'` overlay so unknown keys are rejected.

---

## Data Access

### Spring Data JPA

```java
public interface OrderRepository extends JpaRepository<Order, Long> {
    List<Order> findByStatus(String status);
    List<Order> findByStatusAndCustomerName(String status, String name);

    @Query("SELECT o FROM Order o WHERE o.total > :amount")
    List<Order> findExpensiveOrders(@Param("amount") BigDecimal amount);
}
```

### PyFly Data

```python
@repository
class OrderRepository(Repository[Order, int]):
    async def find_by_status(self, status: str) -> list[Order]: ...
    async def find_by_status_and_customer_name(self, status: str, name: str) -> list[Order]: ...

    @query("SELECT o FROM orders WHERE o.total > :amount")
    async def find_expensive_orders(self, amount: float) -> list[Order]: ...
```

Just like Spring Data JPA, you declare the repository by subclassing `Repository[T, ID]` with concrete type parameters. The entity type and ID type are extracted automatically via `__init_subclass__` — no explicit `__init__` or model passing needed. The `AsyncSession` is auto-configured and injected by the container.

**Derived queries** work the same way: define a method signature following the naming convention (`find_by_<field>_and_<field>`), and PyFly generates the query at startup.

**Key difference:** Spring Data uses Java interfaces — methods are abstract and Spring generates implementations at runtime via CGLIB proxying. PyFly uses stub method signatures (with `...` as the body) on concrete classes. The framework detects these stubs during `ApplicationContext.start()` and generates the query implementations via `BeanPostProcessor`.

### Specifications (Dynamic Queries)

**Spring:**
```java
Specification<Order> spec = (root, query, cb) ->
    cb.and(
        cb.equal(root.get("status"), "ACTIVE"),
        cb.greaterThan(root.get("total"), 100)
    );
List<Order> results = repository.findAll(spec);
```

**PyFly:**
```python
spec = (
    Specification.where(field="status", op="eq", value="ACTIVE")
    .and_where(field="total", op="gt", value=100)
)
results = await repository.find_all_by_spec(spec)
```

### Pagination

**Spring:** `Page<Order> findAll(Pageable pageable)`

**PyFly:**
```python
page: Page[Order] = await repository.find_all(
    Pageable.of(1, 20, Sort.by("created_at").descending())
)
# page.content, page.total_elements, page.total_pages, page.number
```

`find_all(pageable)` counts the total, applies the `Pageable`'s sort, slices with `LIMIT`/`OFFSET`, and returns a `Page[T]`. PyFly's `Pageable` is **1-based** (`page >= 1`). Import with `from pyfly.data import Pageable, Sort`.

### Entity ↔ DTO Mapping — MapStruct → `Mapper`

**Spring (MapStruct):**
```java
@Mapper
public interface UserMapper {
    @Mapping(source = "username", target = "name")
    UserDTO toDto(User user);
    List<UserDTO> toDtoList(List<User> users);
}
```

**PyFly:**
```python
from pyfly.data import Mapper, mapping, default_mapper

# Imperative
mapper = Mapper()
mapper.add_mapping(User, UserDTO, field_map={"username": "name"}, transformers={"email": str.lower})
dto = mapper.map(user, UserDTO)             # auto name-match + nested-model recursion
dtos = mapper.map_list(users, UserDTO)

# Declarative — config lives next to the types
@mapping(User, UserDTO, rename={"username": "name"}, transform={"email": str.lower})
class UserMapper: ...

dto = default_mapper.map(user, UserDTO)
```

`Mapper` (from `pyfly.data`) maps between dataclasses and Pydantic models by matching field names, with renaming (`field_map`/`rename`), value transformers, field exclusion, projections, and recursion into nested models and collections of models.

**Key difference:** MapStruct generates `*Impl` classes at compile time. PyFly's `Mapper` is a runtime, reflection-based mapper — intentionally no codegen, no generated classes, and no string-expression DSL. It is Pydantic-aware: it keeps nested models as live instances and constructs the destination through its (validating) constructor.

### Read/Write Routing — `AbstractRoutingDataSource` → `RoutingSessionFactory`

**Spring** routes between datasources with `AbstractRoutingDataSource` and `@Transactional(readOnly = true)`. **PyFly** uses `RoutingSessionFactory` plus a `read_only()` context:

```python
from pyfly.data.relational import RoutingSessionFactory, read_only

factory = ctx.get_bean(RoutingSessionFactory)

async def list_users() -> list[User]:
    with read_only():            # routes to the read replica when one is configured
        session = factory()      # AsyncSession from the replica session maker
        ...

async def create_user(data: dict) -> User:
    session = factory()          # outside read_only() -> primary (read/write)
    ...
```

The factory picks the primary or read-replica session maker based on whether the current block is inside `read_only()` (the `@Transactional(readOnly = true)` analogue). Routing is opt-in: with no replica configured, the factory always uses the primary. `factory.primary()` and `factory.replica()` force a specific side regardless of context.

---

## Caching

### Spring Boot

```java
@Cacheable(value = "orders", key = "#id")
public Order findById(Long id) { }

@CacheEvict(value = "orders", key = "#id")
public void deleteOrder(Long id) { }

@CachePut(value = "orders", key = "#order.id")
public Order updateOrder(Order order) { }
```

### PyFly

```python
from pyfly.cache import cacheable, cache_evict, cache_put
from pyfly.cache.adapters.memory import InMemoryCache

cache_backend = InMemoryCache()  # or RedisCacheAdapter

@cacheable(backend=cache_backend, key="order:{id}")
async def find_by_id(self, id: int) -> Order: ...

@cache_evict(backend=cache_backend, key="order:{id}")
async def delete_order(self, id: int) -> None: ...

@cache_put(backend=cache_backend, key="order:{order.id}")
async def update_order(self, order: Order) -> Order: ...
```

The decorator names and behavior map one-to-one. PyFly uses explicit `backend` injection (a `CacheAdapter` instance) rather than named caches. PyFly also provides `@cache` as a simpler alias for `@cacheable`.

**Backend:** Spring auto-configures CacheManager based on classpath. PyFly auto-configures based on installed extras — if `redis` is installed, `RedisCacheAdapter` is used; otherwise `InMemoryCache`. A `PostgresCacheAdapter` (durable SQL-backed cache) is also available via `pyfly.cache.provider=postgres`. All can be overridden in configuration.

---

## Scheduling

### Spring Boot

```java
@Scheduled(fixedRate = 5000)
public void pollExternalService() { }

@Scheduled(cron = "0 0 2 * * ?")
public void nightlyCleanup() { }
```

### PyFly

```python
@scheduled(fixed_rate=5.0)
async def poll_external_service(self) -> None: ...

@scheduled(cron="0 2 * * *")
async def nightly_cleanup(self) -> None: ...
```

**Key differences:**
- Spring uses milliseconds (`5000`); PyFly uses seconds (`5.0`)
- Spring cron uses 6 fields (seconds included); PyFly uses standard 5-field cron (minutes, hours, day, month, weekday)
- PyFly scheduling methods are `async`, so they can perform I/O directly without blocking

---

## Aspect-Oriented Programming

### Spring Boot

```java
@Aspect
@Component
public class LoggingAspect {
    @Before("execution(* com.example.service.*.*(..))")
    public void logBefore(JoinPoint joinPoint) {
        logger.info("Calling: {}", joinPoint.getSignature().getName());
    }

    @Around("@annotation(Timed)")
    public Object measureTime(ProceedingJoinPoint pjp) throws Throwable {
        long start = System.nanoTime();
        Object result = pjp.proceed();
        long duration = System.nanoTime() - start;
        logger.info("Execution took {} ms", duration / 1_000_000);
        return result;
    }
}
```

### PyFly

```python
@aspect
@component
class LoggingAspect:
    @before("execution(* my_service.services.*.*(..))")
    async def log_before(self, join_point: JoinPoint) -> None:
        logger.info(f"Calling: {join_point.method_name}")

    @around("annotation(timed)")
    async def measure_time(self, join_point: JoinPoint) -> Any:
        start = time.perf_counter()
        result = await join_point.proceed()
        duration = time.perf_counter() - start
        logger.info(f"Execution took {duration * 1000:.1f} ms")
        return result
```

**Advice types map directly:**

| Spring | PyFly | When it runs |
|--------|-------|-------------|
| `@Before` | `@before` | Before the target method |
| `@AfterReturning` | `@after_returning` | After successful return |
| `@AfterThrowing` | `@after_throwing` | After an exception |
| `@After` | `@after` | After method (always, like `finally`) |
| `@Around` | `@around` | Wraps the method, controls execution |

**Pointcut expressions:** PyFly supports `execution()` for method pattern matching and `annotation()` for targeting decorated methods. The syntax is simplified compared to Spring's AspectJ pointcut language.

---

## Resilience Patterns

### Spring Boot (with Resilience4j)

```java
@CircuitBreaker(name = "orderService", fallbackMethod = "fallback")
@RateLimiter(name = "orderService")
public Order getOrder(Long id) { }
```

### PyFly

```python
from pyfly.resilience import RateLimiter, rate_limiter, fallback
from pyfly.client import service_client, get

# Rate limiting — construct a RateLimiter, then apply the decorator
limiter = RateLimiter(max_tokens=100, refill_rate=100 / 60)

@rate_limiter(limiter)
async def get_order(self, id: int) -> Order: ...

# Circuit breaker + retry (declarative client)
@service_client(
    base_url="http://order-svc",
    circuit_breaker=True,
    retry=3,
    circuit_breaker_failure_threshold=5,
)
class OrderClient:
    @get("/orders/{id}")
    async def get_order(self, id: int) -> Order: ...

# Fallback
@fallback(fallback_method=get_cached_order)
async def get_order(self, id: int) -> Order: ...
```

**Additional resilience patterns:**

| Pattern | Spring (Resilience4j) | PyFly |
|---------|----------------------|-------|
| Rate Limiter | `@RateLimiter` | `RateLimiter(max_tokens, refill_rate)` + `@rate_limiter(limiter)` |
| Circuit Breaker | `@CircuitBreaker` | `CircuitBreaker(failure_threshold, recovery_timeout)` + `@circuit_breaker(breaker)` |
| Bulkhead | `@Bulkhead` | `Bulkhead(max_concurrent)` + `@bulkhead(bh)` |
| Time Limiter | `@TimeLimiter` | `@time_limiter(timeout=timedelta(...))` |
| Retry | `@Retry` | `@retry(max_attempts, *, delay, backoff, jitter, ...)` |
| Fallback | `fallbackMethod` | `@fallback(fallback_method=...)` / `@fallback(fallback_value=...)` |

---

## Observability

### Spring Boot Actuator

```yaml
management:
  endpoints:
    web:
      exposure:
        include: health,info,metrics
```

### PyFly Actuator

```yaml
pyfly:
  web:
    actuator:
      enabled: true
```

| Spring Actuator | PyFly Actuator |
|----------------|----------------|
| `/actuator/health` | `/actuator/health` |
| `/actuator/info` | `/actuator/info` |
| `/actuator/beans` | `/actuator/beans` |
| `/actuator/env` | `/actuator/env` |

### Metrics

**Spring:** Uses Micrometer (Prometheus registry, `@Timed` annotation)

**PyFly:** Uses `MetricsRegistry` with Prometheus backend:

```python
from pyfly.observability import timed, counted, MetricsRegistry

@timed("order_service_find")
async def find_order(self, id: int) -> Order: ...

@counted("orders_created")
async def create_order(self, data: dict) -> Order: ...
```

---

## Messaging

### Spring Boot (with Spring Kafka)

```java
@KafkaListener(topics = "orders", groupId = "order-service")
public void handleOrder(OrderEvent event) { }

@Autowired  // Spring field injection
private KafkaTemplate<String, OrderEvent> kafkaTemplate;

public void publishOrder(OrderEvent event) {
    kafkaTemplate.send("orders", event);
}
```

### PyFly

```python
from pyfly.messaging import message_listener
from pyfly.eda import event_publisher, EventEnvelope

@message_listener(topic="orders", group_id="order-service")
async def handle_order(self, event: dict) -> None: ...

@event_publisher
class OrderPublisher:
    async def publish_order(self, event: EventEnvelope) -> None:
        await self._event_bus.publish(event)
```

**Key difference:** PyFly separates **messaging** (Kafka/RabbitMQ transport) from **events** (domain event bus). The messaging module handles broker communication; the EDA module provides the event bus abstraction. This means you can publish domain events within a monolith using `InMemoryEventBus` and later switch to Kafka, RabbitMQ, Redis Streams, or Postgres by changing the adapter (`pyfly.eda.provider`) — no code changes needed.

---

## Distributed Transactions

### Spring Boot (fireflyframework-transactional-engine)

```java
@Saga(name = "create-order", layerConcurrency = 5)
@Component
public class CreateOrderSaga {

    @SagaStep(
        id = "reserve-inventory",
        compensate = "releaseInventory",
        retry = 3,
        backoffMs = 100,
        timeoutMs = 5000
    )
    public Mono<ReservationResult> reserveInventory(
        @Input OrderRequest request,
        SagaContext ctx
    ) {
        return inventoryService.reserve(request.getItems());
    }

    public Mono<Void> releaseInventory(
        @FromStep("reserve-inventory") ReservationResult result
    ) {
        return inventoryService.release(result);
    }
}
```

### PyFly

```python
@saga(name="create-order", layer_concurrency=5)
@component
class CreateOrderSaga:

    @saga_step(
        id="reserve-inventory",
        compensate="release_inventory",
        retry=3,
        backoff_ms=100,
        timeout_ms=5000,
    )
    async def reserve_inventory(
        self,
        request: Annotated[OrderRequest, Input],
        ctx: SagaContext,
    ) -> ReservationResult:
        return await self.inventory_service.reserve(request.items)

    async def release_inventory(
        self,
        result: Annotated[ReservationResult, FromStep("reserve-inventory")],
    ) -> None:
        await self.inventory_service.release(result)
```

**Key differences:**
- **Reactive vs async/await:** Java uses Project Reactor (`Mono<T>`, `Flux<T>`). PyFly uses native `async/await` with `asyncio.gather`, `asyncio.Semaphore`, and `asyncio.wait_for`
- **Annotations vs decorators:** Java uses `@SagaStep(id = "...")`. PyFly uses `@saga_step(id="...")`
- **Parameter injection:** Java uses `@Input`, `@FromStep`. PyFly uses `typing.Annotated` with marker classes: `Annotated[T, Input]`, `Annotated[T, FromStep("step-id")]`
- **Configuration:** Java uses `@ConfigurationProperties`. PyFly uses `@config_properties` with YAML binding

### TCC Pattern

**Java:**
```java
@Tcc(name = "order-payment")
@Component
public class OrderPaymentTcc {
    @TccParticipant(id = "payment-service", order = 1)
    public class PaymentParticipant {
        @TryMethod
        public Mono<ReservationId> tryReserve(@Input PaymentRequest request) { }
        @ConfirmMethod
        public Mono<Void> confirm(@FromTry ReservationId id) { }
        @CancelMethod
        public Mono<Void> cancel(@FromTry ReservationId id) { }
    }
}
```

**PyFly:**
```python
@tcc(name="order-payment")
@component
class OrderPaymentTcc:
    @tcc_participant(id="payment-service", order=1)
    class PaymentParticipant:
        @try_method
        async def try_reserve(self, request: Annotated[PaymentRequest, Input]) -> ReservationId: ...
        @confirm_method
        async def confirm(self, id: Annotated[ReservationId, FromTry]) -> None: ...
        @cancel_method
        async def cancel(self, id: Annotated[ReservationId, FromTry]) -> None: ...
```

The patterns map one-to-one. See the [Transactional Engine Guide](modules/transactional.md) for complete documentation.

---

## Server Abstraction

### Spring Boot

Spring Boot embeds a servlet/reactive container (Tomcat, Jetty, or Undertow) selected via classpath detection. The `WebServer` interface abstracts the embedded server, and `EventLoopGroup` (in Spring WebFlux with Netty) manages the I/O runtime.

```yaml
server:
  port: 8080
  tomcat:
    threads:
      max: 200
```

### PyFly

PyFly follows the same pattern with `ApplicationServerPort` for ASGI servers and `EventLoopPort` for event loop policies. Server selection cascades by priority: Granian > Uvicorn > Hypercorn, mirroring Spring's Tomcat > Jetty > Undertow ordering.

```yaml
pyfly:
  web:
    port: 8080
  server:
    type: auto            # auto | granian | uvicorn | hypercorn
    event-loop: auto      # auto | uvloop | winloop | asyncio
    workers: 0            # 0 = cpu_count
    granian:
      runtime-threads: 1
```

| Spring Boot | PyFly | Purpose |
|-------------|-------|---------|
| `WebServer` interface | `ApplicationServerPort` protocol | Contract for the embedded server |
| `EventLoopGroup` (Netty) | `EventLoopPort` protocol | Contract for the I/O runtime |
| `server.port` | `pyfly.web.port` | HTTP listen port |
| `server.tomcat.*` | `pyfly.server.granian.*` | Server-specific tuning |
| Tomcat (default) | Granian (default) | Highest-priority server |
| Jetty (fallback) | Uvicorn (fallback) | Ecosystem-standard fallback |
| Undertow (alternative) | Hypercorn (alternative) | Advanced protocol support (HTTP/3) |

**Key similarity:** Both frameworks use conditional bean registration to cascade through server implementations. The first matching auto-configuration wins, and users can always override by providing their own bean.

**Key difference:** Spring Boot runs the server inside the JVM process. PyFly's servers (Granian, Uvicorn) are external ASGI servers that host the Python application. Granian's Rust/tokio runtime provides C-level performance for HTTP parsing without the GIL overhead that Python-only servers face.

---

## Integration Testing with Containers

### Spring Boot

```java
@SpringBootTest
@Testcontainers
class OrderRepositoryTest {
    @Container
    @ServiceConnection
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");
}
```

`@Testcontainers` manages the container lifecycle and `@ServiceConnection` wires its connection details into the application context.

### PyFly

```python
from pyfly.testing.testcontainers import postgres_container, pyfly_config, requires_docker

@requires_docker   # skips cleanly when no Docker daemon is reachable
def test_with_real_postgres():
    with postgres_container() as pg:                # postgres:16-alpine by default
        config = pyfly_config(pg)                   # the @ServiceConnection equivalent
        # config now carries pyfly.data.relational.url -> the container's async URL
        ...
```

`pyfly.testing.testcontainers` is the `@Testcontainers` / `@ServiceConnection` equivalent: it spins up a real Postgres/MySQL/Redis/MongoDB/Kafka in Docker, then maps each started container's connection details straight into pyfly config keys.

| Spring | PyFly | Purpose |
|--------|-------|---------|
| `@Testcontainers` | `with postgres_container() as pg:` | Container lifecycle (context-managed) |
| `@ServiceConnection` | `pyfly_config(pg)` / `pyfly_config_for(pg)` | Map connection details into config |
| `PostgreSQLContainer` | `postgres_container()` | Postgres (async URL rewritten to `asyncpg`) |
| `MySQLContainer` | `mysql_container()` | MySQL (rewritten to `aiomysql`) |
| `GenericContainer` (Redis) | `redis_container()` | Redis (cache + session URLs) |
| `MongoDBContainer` | `mongodb_container()` | MongoDB |
| `KafkaContainer` | `kafka_container()` | Kafka |
| `@DynamicPropertySource` | `pyfly_config(*containers, base=...)` | One-call Config for several containers |

Requires the extra and a running Docker daemon: `pip install 'pyfly[testcontainers]'`. Use `@requires_docker` (or `is_docker_available()`) to skip integration tests cleanly where Docker is absent.

---

## More Spring-parity features (v26.06.37–55)

This wave closed several remaining gaps with Spring / Spring Cloud. Each feature is
covered in depth in its module guide; the table below maps the Spring concept to its
PyFly equivalent.

### Scopes and refresh (DI)

| Spring | PyFly | Notes |
|--------|-------|-------|
| `@SessionScope` / `scope="session"` | `@component(scope=Scope.SESSION)` | One instance per `HttpSession`, stored as a session attribute; needs the session module enabled. See [DI Guide](modules/dependency-injection.md#session). |
| Custom `Scope` SPI (`registerScope`) | `Container.register_scope(name, handler)` + `scope="<name>"` | Implement the `ScopeHandler` protocol (`get` / `remove`); built-in names are reserved. |
| `@RefreshScope` (Spring Cloud) | `@refresh_scope` / `scope="refresh"` | Refresh-scoped beans are evicted and rebuilt on refresh; the `"refresh"` scope is built in. |
| `ContextRefresher.refresh()` + `RefreshScopeRefreshedEvent` | `ContextRefresher.refresh()` (injectable) + `RefreshScopeRefreshedEvent` | Evicts refresh-scoped beans, resets `@config_properties`, returns/publishes the evicted keys. |

```python
from pyfly.container import component, refresh_scope, Scope
from pyfly.context import ContextRefresher

@refresh_scope          # OUTER decorator
@component
class FeatureFlags: ...

@component(scope=Scope.SESSION)
class ShoppingCart: ...
```

### More conditional beans

| Spring | PyFly | Notes |
|--------|-------|-------|
| `@ConditionalOnSingleCandidate` | `@conditional_on_single_candidate(T)` | Registers when exactly one candidate of `T` exists (or one is `@primary`). Evaluated in pass 2. |
| `@ConditionalOnWebApplication` | `@conditional_on_web_application()` | Registers only when `starlette` or `fastapi` is importable. Evaluated in pass 1. |
| `@ConditionalOnResource` | `@conditional_on_resource(path)` | Registers only when a filesystem path exists. Evaluated in pass 1. |

### Boolean profile expressions

| Spring | PyFly | Notes |
|--------|-------|-------|
| `@Profile("prod & cloud")` / `&` `\|` `!` `()` | `profile="prod & cloud"` (and on `@bean`) | Spring Boot 2.4+ boolean operators plus grouping, via `Environment.accepts_profiles()`. The legacy comma-OR form still works. Parsed with `ast`, never `eval`. See [Configuration Guide](modules/configuration.md#boolean-profile-expressions). |

### Application events

| Spring | PyFly | Notes |
|--------|-------|-------|
| `ApplicationEventPublisher` (injectable) | `ApplicationEventPublisher` (injectable) | A singleton bean wired to the `ApplicationEventBus`; `await publisher.publish(event)`. |
| `@EventListener` on arbitrary objects | `@app_event_listener` on any annotated type | The event type need not subclass `ApplicationEvent`; dispatch is by `isinstance`, and sync listeners are allowed. See [Events Guide](modules/events.md#applicationeventpublisher-injectable). |

### Method-level security

| Spring | PyFly | Notes |
|--------|-------|-------|
| `@PreAuthorize` / `@PostAuthorize` SpEL | `@pre_authorize` / `@post_authorize` | Share the SpEL subset (`hasRole`, `hasAnyRole`, `hasAuthority`, `hasAnyAuthority`, `hasPermission`, `isAuthenticated`, `isAnonymous`, `permitAll`, `denyAll`, `principal`, `authentication`), bare or called, with comparisons and `and`/`or`/`not`. AST-walked, no `eval`. |
| `#paramName` / `returnObject` | `#paramName` / `returnObject` | Method args are bound by name; `returnObject` is available in `@post_authorize`. (`@secure` does not bind args.) |
| `RoleHierarchy` bean | `RoleHierarchy` + `set_role_hierarchy()` / `get_role_hierarchy()` | `RoleHierarchy.from_string("ADMIN > USER")`, `expand(roles)`; one process-wide hierarchy consulted by `hasRole`/`hasAnyRole`/`hasAuthority`. See [Security Guide](modules/security.md#method-level-security). |

### OAuth2 PKCE

| Spring | PyFly | Notes |
|--------|-------|-------|
| `ClientRegistration` PKCE (`code_challenge`/S256) | `ClientRegistration(use_pkce=True)` | Toggling `use_pkce` makes `OAuth2LoginHandler` generate a `code_verifier`/`code_challenge` (S256) on the `authorization_code` flow. No extra wiring. See [Security Guide](modules/security.md#pkce-proof-key-for-code-exchange). |

### Distributed trace propagation (OTel)

| Spring | PyFly | Notes |
|--------|-------|-------|
| Sleuth / Micrometer Tracing W3C propagation | `TracingFilter` (inbound) + `HttpxClientAdapter` (outbound) | W3C `traceparent` is extracted into a SERVER span and injected on outbound httpx calls; logs gain `trace_id`/`span_id` via the `StructlogAdapter`. Safe no-op without OpenTelemetry. See [Observability Guide](modules/observability.md#distributed-trace-propagation). |
| MDC `traceId`/`spanId` | `trace_id` / `span_id` log fields | Stamped automatically by `get_logger(...)`. |

### Conditional caching

| Spring | PyFly | Notes |
|--------|-------|-------|
| `@Cacheable(condition=…, unless=…)` | `@cacheable(..., condition=…, unless=…)` | `condition` (over call args) bypasses the cache; `unless` (over the result) returns without storing. Keyword-only. See [Caching Guide](modules/caching.md#conditional-caching-condition-and-unless). |

### Resilience tuning

| Spring (Resilience4j) | PyFly | Notes |
|-----------------------|-------|-------|
| `@Retry` (backoff, jitter, retryExceptions) | `@retry(max_attempts, *, delay, backoff, max_delay, jitter, exceptions)` | Sync + async; exponential backoff with optional cap and jitter; only listed exceptions retry. |
| `@CircuitBreaker` (count- / rate-based, half-open) | `CircuitBreaker(...)` + `@circuit_breaker(breaker)` | `failure_threshold` (consecutive) or `failure_rate_threshold`+`window_size` (rate over window); `half_open_max_calls` trial calls; raises `CircuitBreakerException` from `before_call()`. See [Resilience Guide](modules/resilience.md#retry). |

### Scheduling: zones and distributed locking

| Spring | PyFly | Notes |
|--------|-------|-------|
| `@Scheduled(cron=…, zone=…)` | `@scheduled(cron=…, zone="America/New_York")` | IANA zone, UTC default; ignored for `fixed_rate`/`fixed_delay`. `CronExpression` also accepts the 6-field (seconds-first) Spring form and `?`. |
| ShedLock / `@SchedulerLock` | `@scheduled(..., lock=True\|"name", lock_ttl=…)` + `DistributedLock` bean | Skips a tick when the named lock is held elsewhere; defaults to in-process `LocalLock`; register a `DistributedLock` (e.g. Redis) for cross-process single-firing. See [Scheduling Guide](modules/scheduling.md#lock-distributed-locking). |

### Messaging: retry and dead-letter

| Spring (Spring Kafka) | PyFly | Notes |
|-----------------------|-------|-------|
| `@RetryableTopic` / `DefaultErrorHandler` DLT | `@message_listener(..., retries=, retry_delay=, dead_letter_topic=)` | Adapter-agnostic linear-backoff retry; on exhaustion the message is re-published to the DLQ with `x-original-topic` / `x-exception` headers. See [Messaging Guide](modules/messaging.md#retry-and-dead-letter-routing). |

### Multiple named datasources

| Spring | PyFly | Notes |
|--------|-------|-------|
| Multiple `DataSource` beans | `NamedDataSources` registry | Declare under `pyfly.data.relational.datasources.<name>`; inject `NamedDataSources` and call `.get("<name>")` for that datasource's `async_sessionmaker`. The primary keeps its own beans. See [Relational Data Guide](modules/data-relational.md#multiple-named-datasources). |

### Test slices

| Spring | PyFly | Notes |
|--------|-------|-------|
| `@WebMvcTest` | `web_slice(*controllers, overrides=…)` → `(context, client)` | Starts a minimal context + `PyFlyTestClient`. |
| `@DataJpaTest` / `@SpringBootTest` slices | `data_slice(...)` / `service_slice(...)` → `context` | Intent-named aliases of `slice_context`; `overrides` accept a class or a pre-built instance; fail-fast on missing collaborators. See [Testing Guide](modules/testing.md#functional-slices-web_slice--service_slice--data_slice). |

### Session concurrency control

| Spring Security | PyFly | Notes |
|-----------------|-------|-------|
| `maximumSessions` / `SessionRegistry` | `ConcurrencyControlPolicy` + `SessionRegistry` / `InMemorySessionRegistry` + `SessionConcurrencyController` | Caps concurrent sessions per principal at OAuth2 login; `evict-oldest` or `reject-new`. Enable with `pyfly.session.concurrency.enabled=true`. See [Session Guide](modules/session.md#concurrency-control). |

### Actuator refresh endpoint

| Spring Cloud | PyFly | Notes |
|--------------|-------|-------|
| `POST /actuator/refresh` | `POST /actuator/refresh` | Triggers a context refresh (evicts refresh-scoped beans, resets `@config_properties`), returns the refreshed bean keys. Opt-in via `pyfly.management.endpoints.web.exposure.include`. See [Actuator Guide](modules/actuator.md#refresh-endpoint). |

---

## Quick Reference Table

A complete mapping of Spring Boot concepts to PyFly equivalents:

| Spring Boot | PyFly | Notes |
|-------------|-------|-------|
| `@SpringBootApplication` | `@pyfly_application` | Entry point decorator |
| `@Component` | `@component` | Generic managed bean |
| `@Service` | `@service` | Business logic |
| `@Repository` | `@repository` | Data access |
| `@RestController` | `@rest_controller` | REST endpoints |
| `@Configuration` + `@Bean` | `@configuration` + `@bean` | Bean factories |
| `@Autowired` | Constructor injection (automatic) + `Autowired()` field injection | Type-hint based |
| `@Qualifier` | `Qualifier("name")` with `Annotated` | Named bean selection |
| `@Primary` | `@primary` / `@bean(primary=True)` | Default implementation |
| `@Value("${...}")` / `#{...}` | `Value("${...}")` / `Value("#{...}")` | Config-value injection (SpEL-lite) |
| `ObjectFactory<T>` / `Provider<T>` | `Provider[T]` | Deferred / fresh resolution |
| `Map<String, T>` | `dict[str, T]` | Inject all named beans by name |
| `@Lazy` | `@lazy` | Lazy-initialized bean |
| `@SessionScope` | `@component(scope=Scope.SESSION)` | One instance per HTTP session |
| Custom `Scope` SPI | `Container.register_scope(name, handler)` | `ScopeHandler` protocol + `scope="<name>"` |
| `@RefreshScope` (Spring Cloud) | `@refresh_scope` / `scope="refresh"` | Evict + rebuild on refresh |
| `ContextRefresher` / `RefreshScopeRefreshedEvent` | `ContextRefresher` / `RefreshScopeRefreshedEvent` | Trigger + observe a refresh |
| `@Bean @Profile("dev")` | `@bean(profile="dev")` | Profile-scoped factory bean |
| `@Profile("a & b")` boolean ops | `profile="a & b"` (`& \| ! ()`) | Spring Boot 2.4+ profile expressions |
| `@ConditionalOnProperty` | `@conditional_on_property` | Config-based activation |
| `@ConditionalOnClass` | `@conditional_on_class` | Library detection |
| `@ConditionalOnMissingBean` | `@conditional_on_missing_bean` | Missing bean check |
| `@ConditionalOnBean` | `@conditional_on_bean` | Bean presence check |
| `@ConditionalOnSingleCandidate` | `@conditional_on_single_candidate` | Exactly one candidate (or `@primary`) |
| `@ConditionalOnWebApplication` | `@conditional_on_web_application` | Web stack present |
| `@ConditionalOnResource` | `@conditional_on_resource` | Filesystem resource exists |
| `@ConditionalOnExpression` | `@conditional_on_expression` | SpEL-lite expression check |
| `@PostConstruct` | `@post_construct` | Initialization hook |
| `@PreDestroy` | `@pre_destroy` | Cleanup hook |
| `@Order` | `@order` | Execution priority |
| `application.yml` | `pyfly.yaml` | Configuration file |
| `@ConfigurationProperties` | `@config_properties` | Typed config binding |
| Spring Profiles | PyFly Profiles | Environment overlays |
| `@GetMapping` | `@get_mapping` | HTTP GET handler |
| `@PostMapping` | `@post_mapping` | HTTP POST handler |
| `@PutMapping` | `@put_mapping` | HTTP PUT handler |
| `@DeleteMapping` | `@delete_mapping` | HTTP DELETE handler |
| `@PatchMapping` | `@patch_mapping` | HTTP PATCH handler |
| `@RequestBody` | `Body[T]` | JSON request body |
| `@PathVariable` | Plain parameter or `PathVar[T]` | URL path parameter |
| `@RequestParam` | `QueryParam[T]` | Query string parameter |
| `@RequestHeader` | `Header[T]` | HTTP header value |
| `@ControllerAdvice` | `@exception_handler` | Exception handling |
| RFC 7807 `problem+json` | `pyfly.web.problem-details.enabled` | RFC 7807 error responses |
| `@PreAuthorize` / `@PostAuthorize` | `@pre_authorize` / `@post_authorize` | Method-level SpEL security |
| SpEL `#param` / `returnObject` | `#param` / `returnObject` | Bound method args / return value in expressions |
| `RoleHierarchy` bean | `RoleHierarchy` + `set_role_hierarchy()` | Transitive role expansion |
| `ClientRegistration` PKCE | `ClientRegistration(use_pkce=True)` | OAuth2 PKCE (RFC 7636, S256) |
| Jackson `ObjectMapper` | `PyFlyJsonSerializer` + `pyfly.web.json.*` | Global JSON config |
| Jackson serializer/module | `JsonSerializers.register(...)` | Non-Pydantic type encoders |
| `@JsonNaming` (camelCase) | `CamelModel` | Opt-in camelCase model base |
| `HttpMessageConverter` | `MessageConverter` / `MessageConverterRegistry` | Body read/write + negotiation |
| `@Scheduled(fixedRate)` | `@scheduled(fixed_rate)` | Periodic tasks |
| `@Scheduled(cron)` | `@scheduled(cron)` | Cron-based scheduling (5- or 6-field) |
| `@Scheduled(zone=…)` | `@scheduled(cron=…, zone=…)` | IANA time-zone-aware cron |
| ShedLock / `@SchedulerLock` | `@scheduled(lock=…, lock_ttl=…)` + `DistributedLock` | Cluster-wide single-firing |
| `@Cacheable` | `@cacheable` | Method caching |
| `@Cacheable(condition=…, unless=…)` | `@cacheable(condition=…, unless=…)` | Conditional caching (keyword-only) |
| `@CacheEvict` | `@cache_evict` | Cache eviction |
| `@CachePut` | `@cache_put` | Cache update |
| `@Aspect` | `@aspect` | AOP aspect |
| `@Before` | `@before` | Before advice |
| `@After` | `@after` | After advice |
| `@Around` | `@around` | Around advice |
| `@AfterReturning` | `@after_returning` | After-returning advice |
| `@AfterThrowing` | `@after_throwing` | After-throwing advice |
| `JpaRepository` / `ReactiveCrudRepository` | `CrudRepository` → `ReactiveSortingRepository` → `PagingAndSortingRepository` (`RepositoryPort` is an alias of `CrudRepository`); concrete `Repository` / `MongoRepository` | CRUD + sorted/paged operations |
| `findByXAndY` | `find_by_x_and_y` | Derived queries |
| `@Query` | `@query` | Custom queries |
| `Specification` | `Specification` | Dynamic query predicates |
| `Page<T>` | `Page[T]` | Paginated results |
| `Pageable` | `Pageable` | Pagination request |
| MapStruct `@Mapper` | `Mapper` / `@mapping` | Entity ↔ DTO mapping |
| `AbstractRoutingDataSource` | `RoutingSessionFactory` | Read/write datasource routing |
| `@Transactional(readOnly=true)` | `read_only()` context | Route to read replica |
| Multiple `DataSource` beans | `NamedDataSources` | Secondary datasources by name |
| Actuator `/health` | Actuator `/actuator/health` | Health checks |
| Actuator `/info` | Actuator `/actuator/info` | App metadata |
| Actuator `/beans` | Actuator `/actuator/beans` | Bean registry |
| `POST /actuator/refresh` (Spring Cloud) | `POST /actuator/refresh` | Reload config + refresh-scoped beans |
| Sleuth / Micrometer Tracing (W3C) | `TracingFilter` + `HttpxClientAdapter` | W3C `traceparent` in/out + `trace_id`/`span_id` in logs |
| `CircuitBreaker` | `CircuitBreaker` | Resilience pattern |
| `@RateLimiter` | `rate_limiter` | Rate limiting |
| `@Bulkhead` | `bulkhead` | Concurrency limiting |
| `@TimeLimiter` | `time_limiter` | Operation timeout |
| `@Retry` (backoff / jitter) | `@retry(max_attempts, *, delay, backoff, max_delay, jitter, exceptions)` | Retry with exponential backoff + jitter |
| `@CircuitBreaker` (rate / half-open) | `CircuitBreaker(...)` + `@circuit_breaker` | Count- or rate-based tripping, half-open recovery |
| `KafkaTemplate` | `MessageBrokerPort` | Message publishing |
| `@KafkaListener` | `@message_listener` | Message consumption |
| `@RetryableTopic` / DLT | `@message_listener(retries=, retry_delay=, dead_letter_topic=)` | Listener retry + dead-letter routing |
| `ApplicationEvent` | `EventEnvelope` | Domain events |
| `@EventListener` | `@event_listener` | Event handling |
| `ApplicationEventPublisher` | `ApplicationEventPublisher` | Injectable lifecycle/arbitrary-event publisher |
| Micrometer `@Timed` | `@timed` | Method timing |
| Micrometer `@Counted` | `@counted` | Invocation counting |
| `@Saga` | `@saga` | Saga orchestration class |
| `@SagaStep` | `@saga_step` | Saga step definition |
| `@Input` | `Annotated[T, Input]` | Inject saga input |
| `@FromStep("id")` | `Annotated[T, FromStep("id")]` | Inject prior step result |
| `@Tcc` | `@tcc` | TCC transaction class |
| `@TccParticipant` | `@tcc_participant` | TCC participant definition |
| `@TryMethod` | `@try_method` | TCC try phase |
| `@ConfirmMethod` | `@confirm_method` | TCC confirm phase |
| `@CancelMethod` | `@cancel_method` | TCC cancel phase |
| `@FromTry` | `Annotated[T, FromTry]` | Inject try phase result |
| `CompensationPolicy` | `CompensationPolicy` | Compensation strategy enum |
| `SagaContext` | `SagaContext` | Saga execution context |
| `SagaResult` | `SagaResult` | Saga execution result |
| `WebServer` | `ApplicationServerPort` | Embedded ASGI server contract |
| `EventLoopGroup` (Netty) | `EventLoopPort` | Event loop / I/O runtime |
| `server.port` | `pyfly.web.port` | HTTP listen port |
| `server.tomcat.*` | `pyfly.server.granian.*` | Server-specific tuning |
| `server.jetty.*` | `pyfly.server.uvicorn.*` | Fallback server tuning |
| `server.undertow.*` | `pyfly.server.hypercorn.*` | Alternative server tuning |
| Tomcat (default) | Granian (default) | Highest-priority embedded server |
| Jetty (fallback) | Uvicorn (fallback) | Ecosystem-standard fallback |
| Undertow (alternative) | Hypercorn (alternative) | Advanced protocol support |
| `@EnableScheduling` | `SchedulingAutoConfiguration` | Auto-enabled when `croniter` is installed |
| `TaskScheduler` | `TaskScheduler` | Auto-configured bean |
| `@EnableAspectJAutoProxy` | `AopAutoConfiguration` | Always active, no opt-in needed |
| `SecurityAutoConfiguration` | `JwtAutoConfiguration` + `PasswordEncoderAutoConfiguration` | Split by optional dependency |
| `MetricsAutoConfiguration` | `MetricsAutoConfiguration` | Auto-configured when `prometheus_client` is installed |
| `TracingAutoConfiguration` | `TracingAutoConfiguration` | Auto-configured when `opentelemetry` is installed |
| `ActuatorAutoConfiguration` | `ActuatorAutoConfiguration` + `MetricsActuatorAutoConfiguration` | Split by optional dependency |
| `WebServerFactoryAutoConfiguration` | `ServerAutoConfiguration` | Auto-detects Granian > Uvicorn > Hypercorn |
| `@WebMvcTest` | `web_slice(*controllers)` | Web slice → `(context, client)` |
| `@DataJpaTest` | `data_slice(*beans)` | Data slice → `context` |
| `@SpringBootTest` (focused slice) | `service_slice(*beans)` / `slice_context(...)` | Minimal started context |
| `maximumSessions` / `SessionRegistry` | `SessionConcurrencyController` + `SessionRegistry` | Per-principal session cap |
| `@Testcontainers` | `postgres_container()` (context-managed) | Container lifecycle |
| `@ServiceConnection` | `pyfly_config()` / `pyfly_config_for()` | Wire container into config |
| `@DynamicPropertySource` | `pyfly_config(*containers, base=...)` | Build a Config from containers |

---

*For detailed guides on each topic, see the [Documentation Index](index.md).*
