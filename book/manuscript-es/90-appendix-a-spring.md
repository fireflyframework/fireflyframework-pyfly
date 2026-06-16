<span class="eyebrow">Apéndice A</span>

# Chuleta Spring Boot → PyFly {.chtitle}

Si has puesto en producción servicios con Spring Boot, los conceptos de PyFly te resultarán inmediatamente familiares: estereotipos, inyección por constructor, factorías `@configuration` + `@bean`, vinculación tipada de configuración, consultas derivadas, orquestación de sagas — están todos aquí. Lo que cambia es la sintaxis (decoradores de Python en lugar de anotaciones de Java), el modelo de ejecución (`async/await` nativo en lugar de Project Reactor o hilos de servlet) y un puñado de decisiones de diseño deliberadas para encajar con el Python idiomático. Esta chuleta asocia cada concepto de Spring Boot que ya conoces con su equivalente en PyFly, de modo que puedas empezar a leer y escribir código PyFly sin reaprender la arquitectura desde cero.

---

## Arranque de la aplicación y estereotipos

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@SpringBootApplication` | `@pyfly_application` | Combina `@EnableAutoConfiguration` + `@ComponentScan`. `scan_packages` sustituye al escaneo del classpath — enumera los paquetes explícitamente. |
| `SpringApplication.run(...)` | `PyFlyApplication(App); await app.startup()` | El punto de entrada es asíncrono. |
| `@Component` | `@component` | Bean singleton genérico gestionado. |
| `@Service` | `@service` | Capa de lógica de negocio. |
| `@Repository` | `@repository` | Capa de acceso a datos. |
| `@RestController` | `@rest_controller` | Clase de endpoint de API. No hay `@Controller` (no se renderizan vistas). |
| `@Configuration` | `@configuration` | Clase factoría de beans. |
| `@Bean` | `@bean` | Método factoría dentro de una clase `@configuration`. La anotación del tipo de retorno es el tipo registrado del bean. |
| `@Primary` | `@primary` o `@bean(primary=True)` | Opción por defecto cuando existen varios candidatos. |
| `@Order(N)` | `@order(N)` | Ordenación del ciclo de vida y de la inyección. |
| `@Lazy` | `@lazy` | El bean no se crea hasta su primera resolución. |

---

## Inyección de dependencias

| Spring Boot | PyFly | Notas |
|---|---|---|
| Constructor `@Autowired` (implícito en el Spring moderno) | Constructor normal con parámetros tipados | El contenedor lee las anotaciones de tipo de `__init__` automáticamente. |
| Campo `@Autowired` | `field: T = Autowired()` | `Autowired(required=False)` para campos opcionales. |
| `@Qualifier("name")` | `Annotated[T, Qualifier("name")]` | El `Annotated` de Python transporta el cualificador sin perder el tipo base. |
| Inyección de `Optional<T>` | Parámetro `Optional[T]` | Se resuelve a `None` cuando no hay ningún bean registrado. |
| Inyección de `List<T>` | Parámetro `list[T]` | Recopila todas las implementaciones registradas de `T`. |
| Inyección de `Map<String, T>` | Parámetro `dict[str, T]` | `{nombre-bean: bean}` para cada bean con nombre de tipo `T`. |
| Inyección genérica `Repository<User>` | Parámetro `Repository[User, int]` | El contenedor casa por los argumentos de tipo genérico y respeta `@primary` para resolver empates. |
| `ObjectFactory<T>` / `Provider<T>` | `Provider[T]` | Resolución diferida; cada `.get()` vuelve a resolver — seguro para beans `TRANSIENT`. |

!!! tip "Prefiere la inyección por constructor"
    La inyección por constructor mantiene las dependencias visibles en la firma de la clase, evita errores por dependencias ausentes en el arranque en lugar de en tiempo de ejecución, y te permite escribir pruebas unitarias en Python puro sin contenedor: `svc = WalletService(repo=MockRepo(), events=MockEvents())`.

---

## Condiciones y autoconfiguración

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@ConditionalOnProperty` | `@conditional_on_property` | Registra cuando una clave de configuración es igual a un valor concreto. |
| `@ConditionalOnClass` | `@conditional_on_class("module")` | Registra cuando un módulo de Python es importable. |
| `@ConditionalOnMissingBean` | `@conditional_on_missing_bean(T)` | Registra cuando aún no existe ningún bean de tipo `T`. |
| `@ConditionalOnBean` | `@conditional_on_bean(T)` | Registra solo si hay presente un bean de tipo `T`. |
| `@ConditionalOnSingleCandidate` | `@conditional_on_single_candidate(T)` | Exactamente un candidato, o uno marcado como `@primary`. |
| `@ConditionalOnWebApplication` | `@conditional_on_web_application()` | Pila web (Starlette/FastAPI) presente. |
| `@ConditionalOnResource` | `@conditional_on_resource(path)` | Existe una ruta del sistema de archivos. |
| `@ConditionalOnExpression` | `@conditional_on_expression("#{...}")` | Expresión tipo SpEL ligera — admite `${key:default}` + aritmética, comparación y booleanos. Analizada como AST, sin `eval`. |

---

## Hooks del ciclo de vida y ámbitos

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@PostConstruct` | `@post_construct` | Se invoca después de la inyección de dependencias; puede ser `async def`. |
| `@PreDestroy` | `@pre_destroy` | Se invoca en el apagado ordenado; puede ser `async def`. |
| Ámbito por defecto (singleton) | Por defecto (singleton) | Una instancia por aplicación. |
| `@SessionScope` | `@component(scope=Scope.SESSION)` | Una instancia por `HttpSession`. |
| SPI de `Scope` personalizado | `Container.register_scope(name, handler)` | Implementa el protocolo `ScopeHandler`. |
| `@RefreshScope` (Spring Cloud) | `@refresh_scope` | Se descarta y se reconstruye al recibir `POST /actuator/refresh`. |
| `ContextRefresher.refresh()` | `ContextRefresher.refresh()` (inyectable) | Descarta los beans de ámbito refresh, reinicia `@config_properties` y devuelve las claves modificadas. |
| `ApplicationEventPublisher` | `ApplicationEventPublisher` (inyectable) | `await publisher.publish(event)`. |
| `@EventListener` | `@app_event_listener` | Despacho por `isinstance`; se permiten listeners síncronos. |

---

## Configuración y perfiles

| Spring Boot | PyFly | Notas |
|---|---|---|
| `application.yml` | `pyfly.yaml` | Misma estructura jerárquica. |
| `application-{profile}.yml` | `pyfly-{profile}.yaml` | Superposiciones por perfil. |
| `spring.profiles.active=dev` | `PYFLY_PROFILES_ACTIVE=dev` (variable de entorno) o `pyfly.profiles.active: dev` en `pyfly.yaml` | La activación es idéntica en el orden de prioridad. |
| `@ConfigurationProperties(prefix=…)` | `@config_properties(prefix=…)` sobre un `@dataclass` (`from pyfly.core import config_properties`) | Respaldado por Pydantic: validado y congelado en el arranque. |
| `@Value("${key}")` | `field: str = Value("${key}")` (`from pyfly.core import Value`) | Lanza una excepción si falta la clave. |
| `@Value("${key:default}")` | `field: str = Value("${key:default}")` | Valor por defecto tras los dos puntos. |
| `@Value("#{expr}")` SpEL | `Value("#{...}")` SpEL ligero | Aritmética, comparación, booleanos, sustitución `${...}` y mapeo `env`. Inyección por constructor: `Annotated[bool, Value("#{...}")]`. |
| `@Bean @Profile("dev")` | `@bean(profile="dev")` | Expresión de perfil (`& \| ! ()`) sobre cualquier `@bean`. |
| `@Profile("prod & cloud")` booleano | `profile="prod & cloud"` | Operadores de Spring Boot 2.4+; la antigua forma de OR con comas sigue funcionando. |
| `spring.application.name` | `pyfly.app.name` | Clave del nombre de la aplicación en `pyfly.yaml`. |

Prioridad de las fuentes de propiedades (de menor → mayor):

1. `pyfly-defaults.yaml` (valores integrados del framework)
2. `pyfly.yaml` (valores por defecto de la aplicación)
3. `pyfly-{profile}.yaml` (superposiciones por perfil)
4. Variables de entorno (en tiempo de ejecución, máxima prioridad)

Esto es idéntico al orden de las fuentes de propiedades de Spring Boot.

---

## Capa web

Imports: `from pyfly.web import (Body, PathVar, QueryParam, Valid,`
`    get_mapping, post_mapping, put_mapping, delete_mapping, patch_mapping,`
`    request_mapping)`. Estereotipos: `from pyfly.container import rest_controller,`
`    service, repository, component, configuration`. 404: `from pyfly.kernel`
`    import ResourceNotFoundException`.

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@RestController` | `@rest_controller` | |
| `@RequestMapping("/path")` | `@request_mapping("/path")` | Prefijo a nivel de clase. |
| `@GetMapping` | `@get_mapping` | Todos los métodos manejadores son `async def`. |
| `@PostMapping` | `@post_mapping` | |
| `@PutMapping` | `@put_mapping` | |
| `@DeleteMapping` | `@delete_mapping` | |
| `@PatchMapping` | `@patch_mapping` | |
| `@PathVariable Long id` | Parámetro `id: PathVar[int]` | Anotación de tipo obligatoria; casa por nombre. |
| `@RequestParam(defaultValue="0") int page` | `page: QueryParam[int] = 0` | El valor por defecto de Python sustituye a `defaultValue`. |
| `@RequestBody @Valid T body` | `body: Valid[Body[T]]` | Deserialización Pydantic + validación combinadas. |
| `@RequestHeader("X-Token") String t` | `t: Header[str]` | |
| `@ResponseStatus(HttpStatus.CREATED)` | `@post_mapping("/", status_code=201)` | Código de estado en el decorador del mapeo. |
| `@ControllerAdvice` + `@ExceptionHandler` | `@exception_handler` o jerarquía de excepciones integrada | `ResourceNotFoundException` → 404, `ValidationException` → 422, etc. |
| `spring.mvc.problemdetails.enabled` | `pyfly.web.problem-details.enabled: true` | Respuestas RFC 7807 `application/problem+json`. |

### JSON y negociación de contenido

| Spring Boot | PyFly | Notas |
|---|---|---|
| `spring.jackson.property-naming-strategy` | `pyfly.web.json.property-naming-strategy` | `as-is` (por defecto) o `camelCase`. |
| `spring.jackson.default-property-inclusion: non_null` | `pyfly.web.json.exclude-none: true` | |
| `ObjectMapper` de Jackson | `PyFlyJsonSerializer` | Frontera central de serialización. |
| `Module` de Jackson / serializador personalizado | `JsonSerializers.register(Type, encode=fn)` | Codificadores para tipos no Pydantic. |
| `@JsonNaming(CamelCase…)` | Clase base `CamelModel` | Modelo camelCase opcional — `order_id` se serializa como `orderId`. |
| `HttpMessageConverter` | `MessageConverter` / `MessageConverterRegistry` | Integrados: JSON (primero) y luego XML; añade conversores personalizados con `registry.add(...)`. |

---

## Acceso a datos

Imports: `from pyfly.data.relational.sqlalchemy import (Repository, BaseEntity, Base,`
`    Specification, transactional, Propagation, Isolation)`
`from pyfly.data import Page, Pageable, Sort`
`from pyfly.data.query import query`
`from pyfly.container import repository`

### Repositorios

| Spring Boot | PyFly | Notas |
|---|---|---|
| `JpaRepository<E, ID>` / `CrudRepository` | `Repository[E, ID]` | `from pyfly.data.relational.sqlalchemy import Repository`; decora con `@repository`. Crea una subclase con parámetros de tipo concretos; el `AsyncSession` se inyecta automáticamente en el arranque. |
| `@Repository interface WalletRepo extends JpaRepository<…>` | `@repository class WalletRepository(Repository[WalletEntity, str])` | Clase, no interfaz; el cuerpo solo contiene esbozos de consultas derivadas y métodos personalizados. |
| `findByOwnerId(String id)` | `async def find_by_owner_id(self, owner_id: str) -> list[WalletEntity]: ...` | El cuerpo esbozado `...` desencadena la compilación por parte de `RepositoryBeanPostProcessor` en el arranque. Prefijos: `find_by_`, `count_by_`, `exists_by_`, `delete_by_`. |
| `@Query("SELECT u FROM User u WHERE …")` | `@query("SELECT u FROM User u WHERE …")` (tipo JPQL) o `@query("SELECT …", native=True)` (SQL en bruto) | `from pyfly.data.query import query`. Los parámetros usan la sintaxis `:name`. |
| `Specification<T>` | `Specification(lambda root, q: q.where(root.status == "ACTIVE"))` | Compón con `&` / `\|` / `~`; ejecuta con `repo.find_all_by_spec(spec)` o `repo.find_all_by_spec_paged(spec, pageable)`. |

### Entidades

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@Entity class Order` | `class Order(Base)` | `from pyfly.data.relational.sqlalchemy import Base`. Registra la tabla en `Base.metadata`. |
| `@Entity` + PK UUID subrogada + columnas de auditoría | `class Order(BaseEntity)` | `from pyfly.data.relational.sqlalchemy import BaseEntity`. Hereda `id: UUID`, `created_at`, `updated_at`, `created_by`, `updated_by` — todas mapeadas como `Mapped`/`mapped_column` de SQLAlchemy 2.0. |
| `@Column` / `@Id` | `id: Mapped[str] = mapped_column(String(64), primary_key=True)` | Columnas tipadas de SQLAlchemy 2.0. `Base` (sin auditoría) deja que la entidad sea dueña de su propio tipo de PK, como hace `WalletEntity` de Lumen con un id de tipo `str`. |
| `@SoftDelete` (Hibernate 6) | `SoftDeleteMixin` + `SoftDeleteRepository` | `from pyfly.data.relational.sqlalchemy import SoftDeleteMixin`. Añade `deleted_at`; el repositorio lo filtra automáticamente. |
| Bloqueo optimista `@Version` | `VersionedMixin` | Añade una columna `version: int`; SQLAlchemy lanza `StaleDataError` en caso de conflicto. |

### Paginación y ordenación

| Spring Boot | PyFly | Notas |
|---|---|---|
| `Pageable` / `PageRequest.of(page, size, Sort.by(…).descending())` | `Pageable.of(page, size, Sort.by("created_at").descending())` | `from pyfly.data import Pageable, Sort`. `Sort.by(*fields)` devuelve orden ascendente; `.descending()` lo invierte todo. |
| `Page<T>` | `Page[T]` | `from pyfly.data import Page`. Atributos: `.items`, `.total`, `.page`, `.size`, `.total_pages`, `.has_next`, `.has_previous`. `.map(fn)` transforma los elementos preservando los metadatos. |
| `page.getContent()` / `page.getTotalElements()` | `page.items` / `page.total` | Nomenclatura de Python; `.total_pages` se deriva como `ceil(total / size)`. |
| `repo.findAll(pageable)` | `await repo.find_all(pageable)` | Devuelve `Page[T]`. |
| `repo.findAll(spec, pageable)` | `await repo.find_all_by_spec_paged(spec, pageable)` | Aplica WHERE, ORDER BY y LIMIT/OFFSET en una sola llamada. |

### Transacciones y `save`

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@Transactional` | `@transactional()` | `from pyfly.data.relational.sqlalchemy import transactional`. Resuelve `_session_factory` a partir de `self`, parchea las instancias de `Repository` inyectadas, confirma en caso de éxito y revierte ante una excepción. |
| `@Transactional(propagation = REQUIRES_NEW)` | `@transactional(propagation=Propagation.REQUIRES_NEW)` | Enum `Propagation` completo: `REQUIRED`, `REQUIRES_NEW`, `SUPPORTS`, `NOT_SUPPORTED`, `NEVER`, `MANDATORY`. |
| `@Transactional(isolation = READ_COMMITTED)` | `@transactional(isolation=Isolation.READ_COMMITTED)` | El enum `Isolation` completo refleja los niveles de JDBC. |
| `@Transactional(readOnly = true)` | `@transactional(read_only=True)` | Enruta a la réplica de lectura a través de `RoutingSessionFactory` y marca la sesión como `read_only`. |
| `repo.save(entity)` | `await repo.save(entity)` | Invoca `session.add` + `flush` + `refresh` — vuelca pero **no** confirma; el `@transactional` que lo rodea confirma. |
| `session.merge(entity)` (upsert) | `await repo.upsert(entity)` *(extiéndelo)* o `session.merge(entity)` + flush | No hay `upsert` integrado — añádelo como método en tu repositorio (como hace `WalletRepository` de Lumen). `session.merge` gestiona tanto INSERT como UPDATE en función de la PK. |
| `AbstractRoutingDataSource` | `RoutingSessionFactory` | `factory.primary()` / `factory.replica()` para forzar un lado. |
| Varios beans `DataSource` | `NamedDataSources` | Configuración: `pyfly.data.relational.datasources.<name>`; inyecta `NamedDataSources` y llama a `.get("<name>")`. |

### Proyecciones y mapeador

| Spring Boot | PyFly | Notas |
|---|---|---|
| Proyección por interfaz `interface OrderSummary { … }` | `@projection class OrderSummary: id: str; status: str` | `from pyfly.data.projection import projection`. Un dataclass concreto (no un `Protocol`), no un proxy de la JDK; se registra con `mapper.register_projection(src, proj)`. |
| `repo.findAll(cls, Projection.class)` | `mapper.project(entity, OrderSummary)` | `from pyfly.data.mapper import Mapper`. |
| `@Mapper` de MapStruct | `Mapper` + decorador `@mapping` | Reflexión en tiempo de ejecución; sin generación de código. `mapper.map(obj, TargetDTO)`, `mapper.map_list(...)`. |

### Migraciones

| Spring Boot | PyFly | Notas |
|---|---|---|
| Migraciones de Flyway | `pyfly.data.migrations.*` | Mismo concepto: scripts SQL versionados que se ejecutan en el arranque. |

---

## Caché

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@Cacheable(value="cache", key="#id")` | `@cacheable(backend=cache, key="item:{id}")` | Sintaxis de plantilla `{param}` en la clave. |
| `@Cacheable(condition=…, unless=…)` | `@cacheable(condition=…, unless=…)` | `condition` omite según los argumentos; `unless` evita almacenar según el resultado. |
| `@CacheEvict` | `@cache_evict(backend=cache, key="item:{id}")` | |
| `@CachePut` | `@cache_put(backend=cache, key="item:{id}")` | Actualiza la caché tras la mutación. |
| `CacheManager` (autoconfigurado) | `InMemoryCache` / `RedisCacheAdapter` (autoconfigurados) | Se selecciona Redis cuando el extra `redis` está instalado; recae en memoria en caso contrario. |

---

## Mensajería y eventos

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@KafkaListener(topics=…, groupId=…)` | `@message_listener(topic=…, group_id=…)` | El manejador es `async def`. |
| `KafkaTemplate.send(topic, event)` | `await publisher.publish(dest, event_type, payload)` | Puerto `EventPublisher` (`from pyfly.eda import EventPublisher`); cambia de adaptador mediante configuración. |
| `@RetryableTopic` / DLT | `@message_listener(retries=3, retry_delay=1.0, dead_letter_topic="…")` | Reintento con retroceso lineal; los mensajes agotados se enrutan a la DLQ con cabeceras `x-original-topic` / `x-exception`. |
| `ApplicationEvent` | `EventEnvelope` | Contenedor de eventos de dominio. |
| `@EventListener` | `@event_listener(event_types=["TypeName"])` | Manejador EDA en proceso; `event_type` es la cadena con el nombre de la clase. No existe `@domain_event_listener`. |
| `ApplicationEventPublisher` | `ApplicationEventPublisher` (inyectable) | `await publisher.publish(event)` para eventos de aplicación al estilo Spring. |

!!! tip "Mensajería frente a EDA"
    PyFly separa la **mensajería por broker** (`pyfly.messaging` — transporte Kafka/RabbitMQ) de los **eventos de dominio** (`pyfly.eda` — `EventEnvelope` + `EventBus`). Empieza con `InMemoryEventBus` dentro de un monolito; cambia a un adaptador de Kafka más adelante modificando una sola clave de configuración, no tus manejadores.

---

## Seguridad

| Spring Boot | PyFly | Notas |
|---|---|---|
| `SecurityAutoConfiguration` | `JwtAutoConfiguration` + `PasswordEncoderAutoConfiguration` | Dividido por dependencia opcional. |
| Cadena de filtros JWT | `JwtAutoConfiguration` autocableado | Actívalo con `pyfly.security.jwt.enabled: true`. |
| `@PreAuthorize("hasRole('ADMIN')")` | `@pre_authorize("hasRole('ADMIN')")` | Mismo subconjunto de SpEL: `hasRole`, `hasAnyRole`, `hasAuthority`, `isAuthenticated`, `permitAll`, `denyAll`, `#param`, `and`/`or`/`not`. Recorrido por AST, sin `eval`. |
| `@PostAuthorize("returnObject.owner == principal")` | `@post_authorize("returnObject.owner == principal")` | `returnObject` se vincula al valor de retorno del método. |
| Bean `RoleHierarchy` | `RoleHierarchy.from_string("ADMIN > USER")` + `set_role_hierarchy(...)` | `expand(roles)` es consultado por `hasRole`/`hasAnyRole`. |
| `ClientRegistration` (OAuth2 con código de autorización) | `ClientRegistration(...)` | PKCE: añade `use_pkce=True` — genera `code_verifier`/`code_challenge` (S256) automáticamente. |
| `maximumSessions` / `SessionRegistry` | `SessionConcurrencyController` + `SessionRegistry` | Limita las sesiones concurrentes por principal (`evict-oldest` o `reject-new`). Actívalo: `pyfly.session.concurrency.enabled: true`. |

---

## Programación

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@Scheduled(fixedRate = 5000)` | `@scheduled(fixed_rate=5.0)` | Spring usa milisegundos; PyFly usa **segundos**. |
| `@Scheduled(fixedDelay = 1000)` | `@scheduled(fixed_delay=1.0)` | |
| `@Scheduled(cron = "0 0 2 * * ?")` | `@scheduled(cron="0 2 * * *")` | Spring: 6 campos (segundos primero). PyFly: 5 campos estándar; también acepta la forma de 6 campos de Spring y `?`. |
| `@Scheduled(cron=…, zone=…)` | `@scheduled(cron=…, zone="America/New_York")` | Zona horaria IANA; se ignora para `fixed_rate`/`fixed_delay`. |
| ShedLock / `@SchedulerLock` | `@scheduled(lock=True, lock_ttl=30)` + bean `DistributedLock` | Omite un tick cuando el bloqueo está retenido en otro lugar. Por defecto usa el `LocalLock` en proceso; registra un `DistributedLock` de Redis para un disparo único entre procesos. |
| `@EnableScheduling` | `SchedulingAutoConfiguration` | Se activa automáticamente cuando `croniter` está instalado. No hace falta `@Enable…` explícito. |

---

## Resiliencia

`from pyfly.resilience import retry, CircuitBreaker, circuit_breaker,`
`    RateLimiter, rate_limiter, Bulkhead, bulkhead, time_limiter, fallback`

| Spring (Resilience4j) | PyFly | Notas |
|---|---|---|
| `@Retry` | `@retry(max_attempts=3, *, delay=0.1, backoff=2.0, jitter=0.1, exceptions=(IOError,))` | `delay`/`backoff`/`jitter` son solo por palabra clave. `jitter` es una fracción float en `[0,1]`. |
| `@CircuitBreaker` | `breaker = CircuitBreaker(...); @circuit_breaker(breaker)` | Pasa una *instancia* de `CircuitBreaker`. Basado en conteo (`failure_threshold`) o en tasa (`failure_rate_threshold` + `window_size`). |
| `@RateLimiter` | `limiter = RateLimiter(max_tokens=100, refill_rate=100/60); @rate_limiter(limiter)` | Cubo de tokens; pasa una *instancia*. |
| `@Bulkhead` | `bh = Bulkhead(max_concurrent=10); @bulkhead(bh)` | Límite de concurrencia; pasa una *instancia*. |
| `@TimeLimiter` | `@time_limiter(timeout=timedelta(seconds=2))` | Lanza `asyncio.TimeoutError` al superarse. |
| `fallbackMethod` | `@fallback(fallback_method=fn)` o `@fallback(fallback_value=v)` | Valor de reserva estático o invocable. |

---

## AOP

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@Aspect` + `@Component` | `@aspect` + `@component` | |
| `@Before("execution(…)")` | `@before("execution(…)")` | |
| `@After` | `@after` | Se ejecuta siempre (como `finally`). |
| `@Around` | `@around` | Llama a `await join_point.proceed()` para continuar. |
| `@AfterReturning` | `@after_returning` | |
| `@AfterThrowing` | `@after_throwing` | |
| `@EnableAspectJAutoProxy` | `AopAutoConfiguration` | Siempre activo; no hace falta optar por él. |

DSL de pointcuts: `execution(* pkg.services.*.*(..))` para patrones de métodos; `annotation(timed)` para casar por decorador.

---

## Observabilidad y Actuator

| Spring Boot | PyFly | Notas |
|---|---|---|
| `management.endpoints.web.exposure.include` | `pyfly.management.endpoints.web.exposure.include` | |
| `/actuator/health` | `/actuator/health` | |
| `/actuator/info` | `/actuator/info` | |
| `/actuator/beans` | `/actuator/beans` | |
| `/actuator/env` | `/actuator/env` | |
| `POST /actuator/refresh` (Spring Cloud) | `POST /actuator/refresh` | Descarta los beans de ámbito refresh; reinicia `@config_properties`; devuelve las claves modificadas. |
| `@Timed` de Micrometer | `@timed("metric_name")` | Histograma de tiempos del método. |
| `@Counted` de Micrometer | `@counted("metric_name")` | Contador de invocaciones. |
| `MetricsRegistry` de Prometheus | `MetricsRegistry` (backend de Prometheus) | Autoconfigurado cuando `prometheus_client` está instalado. |
| Sleuth / Micrometer Tracing (W3C) | `TracingFilter` (entrante) + `HttpxClientAdapter` (saliente) | El `traceparent` W3C se extrae en un span SERVER; se inyecta en las llamadas httpx salientes. `trace_id`/`span_id` quedan estampados en los logs vía `StructlogAdapter`. No-op seguro sin OpenTelemetry. |

---

## CQRS y sagas

`from pyfly.cqrs import (Command, CommandHandler, DefaultCommandBus,`
`    Query, QueryHandler, DefaultQueryBus, command_handler, query_handler)`
`from pyfly.transactional.saga.annotations import (saga, saga_step, Input, FromStep)`

| Spring Boot / Axon | PyFly | Notas |
|---|---|---|
| `@CommandHandler` | `@command_handler` + `@service` apilados | Ambos decoradores son obligatorios; `@service` registra el bean. Sobrescribe `do_handle(self, cmd)`. |
| `@QueryHandler` | `@query_handler` + `@service` apilados | Misma regla. Sobrescribe `do_handle(self, qry)`. Método del bus: `.query(...)`. |
| Inyección del bus en el controlador | `commands: DefaultCommandBus, queries: DefaultQueryBus` | Inyecta las clases concretas, no el protocolo. `commands.send(cmd)`, `queries.query(qry)`. |
| `@Repository` + puerto | `@repository` sobre una clase que hereda del puerto `Protocol` con `@runtime_checkable` | El puerto es un `Protocol`; la clase adaptadora lo hereda. |
| `@EventHandler` (event sourcing) | `@event_handler` | Procede del `EventStore`. |
| `@Saga` | `@saga(name="…", layer_concurrency=N)` + `@service` | Ambos decoradores son obligatorios para la inyección de dependencias + el registro en el motor. |
| `@SagaStep(id=…, compensate=…)` | `@saga_step(id="…", compensate="method_name")` | |
| `@Input` | `Annotated[T, Input()]` | El marcador es una **instancia**: `Input()`. Inyecta la carga inicial de la saga. |
| `@FromStep("id")` | `Annotated[T, FromStep("id")]` | El marcador es una **instancia**: `FromStep("step-id")`. Inyecta el resultado de un paso anterior. |
| `@Tcc` | `@tcc(name="…")` | Clase de transacción TCC (Try-Confirm-Cancel). |
| `@TccParticipant` | `@tcc_participant(id="…", order=N)` | |
| `@TryMethod` / `@ConfirmMethod` / `@CancelMethod` | `@try_method` / `@confirm_method` / `@cancel_method` | Métodos de las tres fases de TCC. |
| `@FromTry` | `Annotated[T, FromTry]` | Inyecta el resultado de la fase try en confirm/cancel. |

Event sourcing (gestión de estado mediante secuencias de eventos): `from pyfly.eventsourcing import AggregateRoot, EventSourcedRepository`.
La raíz de agregado usa `self.when(EventType, handler_fn)` para registrar manejadores de aplicación.
Datos: `from pyfly.data.relational.sqlalchemy import Base` (requiere `pyfly[data-relational]`).

---

## Pruebas de integración

| Spring Boot | PyFly | Notas |
|---|---|---|
| `@SpringBootTest` | `service_slice(*beans)` / `slice_context(...)` | Contexto mínimo ya iniciado; `overrides` acepta una clase o una instancia ya construida. |
| `@WebMvcTest` | `web_slice(*controllers, overrides=…)` → `(context, client)` | Inicia un contexto mínimo + `PyFlyTestClient`. |
| `@DataJpaTest` | `data_slice(*beans)` → `context` | Slice de la capa de datos. |
| `@Testcontainers` + `@Container` | `with postgres_container() as pg:` | El context manager de Python gestiona el ciclo de vida. |
| `@ServiceConnection` | `pyfly_config(pg)` / `pyfly_config_for(pg)` | Asocia los detalles de conexión del contenedor a las claves de configuración de PyFly. |
| `@DynamicPropertySource` | `pyfly_config(*containers, base=…)` | Un `Config` en una sola llamada para varios contenedores. |
| `PostgreSQLContainer` | `postgres_container()` | La URL se reescribe automáticamente a `asyncpg`. |
| `MySQLContainer` | `mysql_container()` | La URL se reescribe a `aiomysql`. |
| `GenericContainer` (Redis) | `redis_container()` | URLs de caché + sesión cableadas. |
| `KafkaContainer` | `kafka_container()` | |
| `@requires_docker` | `@requires_docker` | Omite la prueba limpiamente cuando el demonio de Docker no está presente. |

Instálalo con: `pip install 'pyfly[testcontainers]'`

---

## Servidor embebido

| Spring Boot | PyFly | Notas |
|---|---|---|
| `server.port` | `pyfly.server.port` | Puerto HTTP de escucha de la aplicación (por defecto 8080). |
| `server.address` | `pyfly.server.host` | Dirección de enlace de la aplicación. |
| `management.server.port` | `pyfly.management.server.port` | Puerto de gestión independiente (actuator + panel de administración); por defecto 9090. |
| Tomcat (por defecto) | Granian (por defecto) | Runtime HTTP en Rust/tokio; máxima prioridad. |
| Jetty (alternativa de reserva) | Uvicorn (alternativa de reserva) | Reserva ASGI estándar del ecosistema. |
| Undertow (alternativa) | Hypercorn (alternativa) | Soporte de protocolos avanzados (HTTP/3). |
| `server.tomcat.*` | `pyfly.server.granian.*` | Ajuste específico del servidor. |
| Interfaz `WebServer` | Protocolo `ApplicationServerPort` | Contrato del servidor ASGI embebido. |
| `EventLoopGroup` (Netty) | Protocolo `EventLoopPort` | Contrato del runtime de E/S. |
| `server.type: auto` | `pyfly.server.type: auto` (por defecto) | Cascada Granian → Uvicorn → Hypercorn. |

!!! tip "Cascada de autoconfiguración"
    La autoconfiguración de PyFly usa el mismo patrón de beans condicionales que Spring Boot: si Granian está instalado, gana `GranianServerAdapter`; si no, se prueba Uvicorn a continuación; luego Hypercorn. Anúlalo en cualquier punto aportando tu propio bean `ApplicationServerPort`.
