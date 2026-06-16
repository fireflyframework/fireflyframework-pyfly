<span class="eyebrow">Apéndice</span>

# Glosario {.chtitle}

**Adaptador (adapter)** — Una clase concreta que implementa un puerto delegando en una biblioteca o tecnología de infraestructura específica (PostgreSQL, Redis, Kafka, etc.). Los adaptadores viven en el borde de la arquitectura hexagonal y pueden intercambiarse sin tocar las capas de dominio o de aplicación. En PyFly, la factoría de sesiones asíncronas de SQLAlchemy, `RedisCacheAdapter` y `KafkaMessageBroker` son todos adaptadores (Capítulos 5, 10, 13).

**Raíz de agregado (aggregate root)** — El único punto de entrada a un grupo de objetos de dominio que deben permanecer consistentes entre sí; todos los cambios de estado se canalizan a través de sus métodos, que imponen las invariantes y emiten eventos de dominio. El código externo carga y guarda solo la raíz, nunca sus objetos internos. En PyFly, `AggregateRoot[ID]` es la clase base con estado almacenado; `Wallet` es la raíz de agregado con estado almacenado de Lumen y `LedgerAccount` es su contrapartida con event sourcing (Capítulos 6, 9).

**AOP (Programación Orientada a Aspectos)** — Una técnica para añadir preocupaciones transversales —registro de logs, métricas, comprobaciones de seguridad— de forma declarativa a los métodos sin modificar su código fuente. Los decoradores de aviso `@aspect` y `@before`/`@after`/`@around` de PyFly implementan AOP; el Capítulo 15 lo usa para asociar observabilidad a cada método de servicio.

**ApplicationContext** — El contenedor de inyección de dependencias central que descubre los beans, resuelve las dependencias y gestiona su ciclo de vida desde el arranque hasta el apagado. Dispara `ContextRefreshedEvent` una vez que todos los beans están cableados y `ContextClosedEvent` en un apagado ordenado. `ApplicationContext` es el equivalente en PyFly del `ApplicationContext` de Spring (Capítulo 2).

**Async/await** — La sintaxis nativa de corrutinas de Python para E/S no bloqueante. PyFly está construido de forma nativamente asíncrona: cada manejador HTTP, método de repositorio, llamada al bus y cliente de servicio se declara `async def` y se planifica en el bucle de eventos. Las llamadas bloqueantes dentro de una función `async def` congelan el bucle y deben evitarse (Capítulo 1).

**Autocableado (autowiring)** — El mecanismo por el cual el contenedor de inyección de dependencias resuelve los argumentos del constructor de un bean únicamente a partir de las anotaciones de tipo, sin necesidad de código de factoría. PyFly inspecciona las firmas de `__init__` en el arranque e inyecta automáticamente los beans coincidentes; la anotación `@primary` selecciona la implementación preferida cuando existen varios candidatos (Capítulo 2).

**Bean** — Cualquier objeto Python que el contenedor de inyección de dependencias crea, cablea y gestiona. Declaras una clase como bean aplicando un decorador de estereotipo (`@service`, `@repository`, `@component`, `@configuration` o `@rest_controller`), o anotando un método de factoría con `@bean` dentro de una clase `@configuration` (Capítulo 2).

**BFF (Backend for Frontend)** — Una capa de pasarela de API que se sitúa frente a varios microservicios y compone sus capacidades en una API enfocada al recorrido del usuario, adaptada a un cliente de frontend específico. En el Capítulo 11, el nivel BFF de Lumen agrega los datos del monedero y de los pagos para que la aplicación móvil haga una sola petición en lugar de dos.

**Contexto delimitado (bounded context)** — Un ámbito a nivel de DDD dentro del cual un modelo de dominio tiene un significado único e inequívoco. Los servicios en una arquitectura de microservicios a menudo se corresponden uno a uno con contextos delimitados: `WalletService` posee el modelo del monedero; `PaymentsService` posee el modelo de pagos. Se necesitan núcleos compartidos o capas anticorrupción cuando dos contextos deben intercambiar datos (Capítulo 6).

**Mamparo (bulkhead)** — Un patrón de resiliencia que limita el número de llamadas concurrentes a un recurso o servicio dependiente concreto, evitando que una dependencia lenta agote el pool de hilos o de corrutinas y degrade rutas no relacionadas. El decorador `@bulkhead(max_concurrent=N)` de PyFly implementa el mamparo basado en semáforo (Capítulo 13).

**Cortacircuitos (circuit breaker)** — Un patrón de resiliencia que cuenta los fallos consecutivos hacia una dependencia remota; una vez que se alcanza el umbral de fallos, el cortacircuitos salta al estado abierto y cortocircuita las llamadas posteriores con un error rápido, dando tiempo a la dependencia para recuperarse antes de reanudar las llamadas. El decorador `@circuit_breaker` y `@service_client` de PyFly inyectan este comportamiento automáticamente (Capítulos 11, 13).

**Comando (CQRS)** — Un dataclass inmutable y congelado que expresa una única intención de escritura —«abrir un monedero», «depositar fondos»— y lleva los datos exactos necesarios para satisfacerla. Los comandos heredan de `Command[R]`, donde `R` es el tipo de retorno del manejador, y fluyen a través del `CommandBus`. Opcionalmente pueden implementar `validate()` y `authorize()` (Capítulo 7).

**CommandBus** — La canalización que recibe un `Command`, ejecuta la validación y la autorización, lo despacha al `CommandHandler` coincidente y luego publica cualquier evento de dominio almacenado en búfer por el agregado. Se registra un manejador (handler) por cada tipo de comando; el bus impone esta restricción en el arranque (Capítulo 7).

**Compensación** — Una operación hacia adelante que revierte semánticamente el efecto de un paso de saga completado previamente cuando un paso posterior falla. A diferencia de un rollback de base de datos, la compensación es una nueva escritura que deshace explícitamente el cambio anterior (por ejemplo, «reembolsar pago» compensa «capturar pago»). Cada `@saga_step` nombra su método de compensación (Capítulo 12).

**Component** — Un estereotipo genérico para un bean gestionado que no encaja en los roles más específicos de `@service`, `@repository` o `@rest_controller`. `@component` registra la clase con el contenedor y habilita la inyección, pero no aporta ningún significado semántico adicional (Capítulo 2).

**Clase de configuración** — Una clase decorada con `@configuration` que agrupa métodos de factoría `@bean`. El contenedor la trata como una fuente de definiciones de beans, llamando a cada método de factoría y registrando el valor de retorno como un bean nombrado y tipado. Se pueden aplicar guardas de perfil y anotaciones condicionales a la clase o a métodos de factoría individuales (Capítulo 3).

**Contenedor (DI)** — Véase *ApplicationContext*.

**Convención sobre configuración** — El principio según el cual unos valores predeterminados sensatos eliminan el código repetitivo: una clase anotada con `@service` queda automáticamente con ámbito singleton, es descubierta por el escaneo de componentes e inyectada por tipo sin ningún XML ni registro explícito. PyFly adopta esto como valor de diseño central, exigiendo anulaciones explícitas solo cuando el valor predeterminado es incorrecto (Capítulo 1).

**CQRS (Segregación de Responsabilidad entre Comandos y Consultas)** — Un patrón arquitectónico que separa las operaciones de escritura (comandos) de las operaciones de lectura (consultas) en rutas de código, buses y, potencialmente, modelos de datos distintos. Las lecturas pueden almacenarse en caché de forma independiente de las escrituras; los manejadores pueden probarse de forma aislada; las preocupaciones transversales se aplican uniformemente por el bus respectivo (Capítulo 7).

**Cola de mensajes fallidos (DLQ)** — Una cola o topic de mensajes dedicado que recibe los mensajes que no pudieron procesarse tras el número configurado de reintentos. El `@message_listener` de PyFly enruta automáticamente los mensajes envenenados a la DLQ, evitando que bloqueen los mensajes sanos (Capítulo 10).

**Inyección de dependencias (DI)** — Un patrón de diseño en el que un objeto declara sus colaboradores como parámetros del constructor y un contenedor externo proporciona las instancias concretas. La inyección de dependencias desacopla la construcción del uso, lo que facilita intercambiar implementaciones y probar clases con dobles (fakes) o mocks (Capítulo 2).

**Evento de dominio** — Un registro inmutable de un hecho de negocio que ya ha ocurrido —«monedero abierto», «fondos depositados»—. Los eventos de dominio son producidos por las raíces de agregado mediante `raise_event()`, publicados a través del puerto `EventPublisher` y consumidos por escuchadores independientes. En event sourcing, son además la fuente de verdad del estado del agregado (Capítulos 6, 8, 9).

**DTO (Objeto de Transferencia de Datos)** — Un objeto plano y serializable usado para transportar datos a través del límite de una capa —entre el controlador HTTP y el servicio, o entre un servicio y su cliente de API— sin exponer los tipos internos del dominio. En PyFly, las subclases de `BaseModel` de Pydantic sirven como DTO de petición/respuesta (Capítulo 4).

**EDA (Arquitectura Orientada a Eventos)** — Un estilo arquitectónico en el que los servicios se comunican publicando y suscribiéndose a eventos en lugar de mediante llamadas síncronas directas. Productores y consumidores están desacoplados: un productor no sabe qué consumidores existen. El `EventPublisher` y `@event_listener` de PyFly son las primitivas de EDA intra-proceso; `MessageBrokerPort` extiende el patrón a través de los límites de proceso (Capítulos 8, 10).

**Entidad** — Un objeto de dominio con una identidad estable que persiste a lo largo del tiempo y a través de los cambios de estado. Dos entidades son iguales si y solo si comparten el mismo `id` no nulo. En PyFly, `Entity[TID]` rastrea la identidad; `BaseEntity` añade columnas de auditoría (`created_at`, `updated_at`) para la capa de persistencia (Capítulos 5, 6).

**Event sourcing (abastecimiento de eventos)** — Una estrategia de persistencia en la que cada cambio de estado se almacena como un evento de dominio inmutable en un flujo de solo adición. El estado actual de un agregado se calcula reproduciendo todos los eventos del flujo. El módulo `pyfly.eventsourcing` de PyFly proporciona `AggregateRoot`, `EventStore`, `EventSourcedRepository`, soporte para instantáneas y un `ProjectionRunner` (Capítulo 9).

**EventEnvelope** — El envoltorio de metadatos que empaqueta la carga útil de un evento de dominio para su entrega: ID del evento, tipo de evento, ID del flujo del agregado, número de secuencia, marca de tiempo, ID de correlación e ID de causalidad. Cada evento que llega a un escuchador llega dentro de un `EventEnvelope`; nunca construyes uno manualmente (Capítulos 8, 9).

**EventStore** — La capa de persistencia de solo adición para un sistema con event sourcing. Registra los eventos de dominio indexados por ID de flujo y número de secuencia, impone concurrencia optimista mediante el token `version` y soporta consultas por rango para la reproducción. PyFly incluye `SQLAlchemyEventStore` e `InMemoryEventStore` (Capítulo 9).

**Arquitectura hexagonal** — Un estilo arquitectónico que coloca la lógica de dominio y de aplicación en el centro, rodeada de puertos (interfaces), con adaptadores en los bordes. El código de negocio depende únicamente de los puertos; los adaptadores implementan esos puertos usando tecnologías específicas. Este es el principio organizador de cada módulo de PyFly (Capítulos 1, 2, 5).

**Idempotencia** — La propiedad por la cual realizar la misma operación varias veces produce el mismo resultado que realizarla una sola vez. Los manejadores idempotentes son esenciales en la mensajería y la compensación de sagas: los reintentos de red o las garantías de entrega al-menos-una-vez implican que un mensaje puede llegar más de una vez. Se usan claves de deduplicación o tokens de idempotencia para detectar y descartar ejecuciones duplicadas (Capítulos 10, 12).

**Migración** — Un script versionado y ordenado que hace evolucionar el esquema de una base de datos relacional sin destruir datos. PyFly integra Alembic para la gestión de migraciones; `pyfly db migrate` autogenera una migración a partir de los cambios en las entidades y `pyfly db upgrade` aplica las migraciones pendientes (Capítulo 5).

**Patrón outbox** — Una técnica para publicar eventos de dominio de forma fiable junto con una escritura en la base de datos: tanto el cambio de estado como los registros de eventos se escriben en la misma transacción local; un proceso de retransmisión en segundo plano lee los eventos no enviados de la tabla outbox y los reenvía al broker. Esto elimina el commit en dos fases entre la base de datos y el broker de mensajes (Capítulos 9, 12).

**Puerto (port)** — Una clase `Protocol` de Python que define la interfaz de la que depende una pieza de lógica de negocio, sin especificar ninguna implementación. El contenedor de inyección de dependencias cablea en el arranque el adaptador concreto que satisface el protocolo. Los puertos habilitan la arquitectura hexagonal y hacen que los adaptadores sean intercambiables sin ningún cambio en la lógica de negocio (Capítulos 1, 2, 5).

**Bean primario** — Cuando varios beans satisfacen el mismo tipo, se prefiere para la inyección el que está anotado con `@primary`. Sin una anotación `@primary`, el contenedor lanza un error de ambigüedad. `@primary` es la forma de designar el adaptador de producción entre varias alternativas (Capítulo 2).

**Perfil** — Una etiqueta de activación nombrada que selecciona qué beans y valores de configuración están activos en tiempo de ejecución. PyFly carga `pyfly-{profile}.yaml` sobre el `pyfly.yaml` base y activa los beans anotados con `@profile("prod")` solo cuando `prod` está en la lista de perfiles activos. El perfil activo se establece mediante `PYFLY_PROFILES_ACTIVE` o `pyfly.yaml` (Capítulo 3).

**Proyección** — Un modelo de lectura derivado del consumo de un flujo de eventos. Una proyección se suscribe a tipos de eventos específicos y construye incrementalmente una vista consultable —una caché de saldos, una tabla de auditoría, un agregado para un panel— sin tocar el modelo de escritura. En event sourcing, `ProjectionRunner` reproduce el `EventStore` para reconstruir las proyecciones desde cero (Capítulos 8, 9).

**Consulta (CQRS)** — Un dataclass inmutable que expresa una intención de lectura —«obtener el saldo del monedero», «listar transacciones»—. Las consultas heredan de `Query[R]` y fluyen a través del `QueryBus`, que puede almacenar los resultados en caché de forma transparente. Las consultas nunca mutan el estado (Capítulo 7).

**QueryBus** — La canalización que recibe una `Query`, opcionalmente devuelve un resultado en caché, la despacha al `QueryHandler` coincidente y opcionalmente almacena el resultado en caché. Separar el bus de consultas del bus de comandos permite un comportamiento transversal diferente —caché, enrutamiento a réplicas de lectura— para las rutas de lectura (Capítulo 7).

**Limitador de tasa (rate limiter)** — Un componente de resiliencia que limita el número de peticiones que un endpoint o cliente de servicio puede aceptar dentro de una ventana de tiempo, evitando la sobrecarga por tráfico en ráfagas. El `RateLimiter(max_tokens=N, refill_rate=r)` + `@rate_limiter(limiter)` de PyFly implementa un limitador de cubo de tokens (Capítulo 13).

**Repositorio** — Una abstracción similar a una colección sobre la capa de persistencia que permite a la aplicación cargar y guardar agregados o entidades sin nada de SQL en el código de negocio. El `CrudRepository[E, ID]` de PyFly proporciona `find_by_id`, `save`, `delete` tipados y ayudantes de consultas derivadas; las implementaciones personalizadas anotadas con `@repository` reemplazan a las predeterminadas en memoria (Capítulos 2, 5).

**Reintento (retry)** — Un patrón de resiliencia que vuelve a ejecutar una operación fallida tras un retardo, hasta un número máximo de intentos configurado. Los reintentos manejan fallos transitorios —breves fallos de red, errores 503 momentáneos— sin intervención del operador. El `@retry(max_attempts=N, backoff=...)` de PyFly implementa retroceso exponencial con jitter; `@service_client` incluye reintentos por defecto (Capítulos 11, 13).

**Saga** — Una secuencia de transacciones locales coordinadas por un orquestador central. Cada paso confirma en la base de datos de su propio servicio; si un paso posterior falla, el orquestador llama a la transacción compensatoria de cada paso ya confirmado en orden inverso. Los decoradores `@saga` y `@saga_step` de PyFly implementan el patrón saga orquestado con un DAG de ejecución en paralelo (Capítulo 12).

**Serialización** — El proceso de convertir un objeto en memoria a un formato de transmisión (JSON, Protobuf, Avro) para respuestas HTTP o publicación de mensajes, y la deserialización en sentido inverso. El bean central `PyFlyJsonSerializer` de PyFly (`from pyfly.web import PyFlyJsonSerializer`) configura una vez la serialización JSON respaldada por Pydantic y la aplica a cada respuesta HTTP y a cada envoltorio de mensaje (Capítulos 4, 10).

**Servicio** — Un bean gestionado decorado con `@service` que alberga la lógica de negocio y orquesta las llamadas a repositorios, publicadores de eventos y otros servicios. Los servicios son la capa de aplicación en la arquitectura hexagonal: traducen los comandos entrantes en operaciones de dominio y persisten los resultados (Capítulo 2).

**Instantánea (snapshot)** — Una copia serializada en un instante concreto del estado de un agregado con event sourcing, almacenada junto al flujo de eventos para acelerar la reproducción. Al cargar, el repositorio restaura la instantánea y reproduce solo los eventos ocurridos después de la versión de la instantánea, acotando el tiempo de reproducción independientemente de la longitud del flujo (Capítulo 9).

**Estereotipo** — Un decorador que registra una clase como bean y señala su rol arquitectónico: `@service`, `@repository`, `@component`, `@configuration` o `@rest_controller`. Todos los estereotipos son técnicamente equivalentes en el contenedor; la diferencia semántica es para los lectores humanos y las herramientas (Capítulo 2).

**TCC (Try-Confirm-Cancel)** — Un patrón de transacciones distribuidas en el que cada participante primero *reserva* un recurso (Try), y luego el coordinador o bien *confirma* todas las reservas o las *cancela* en función de si todos los Try tuvieron éxito. TCC es útil cuando se requiere una semántica de reserva exacta e inmediata —por ejemplo, retener fondos antes de capturar un pago—. Los decoradores `@tcc(name="…")` + `@tcc_participant(id="…", order=N)` de PyFly implementan el protocolo TCC junto al motor de sagas (Capítulo 12).

**Testcontainers** — Una biblioteca que arranca contenedores Docker reales (PostgreSQL, Redis, Kafka) para las pruebas de integración y los detiene cuando la suite termina. El módulo `pyfly.testing` de PyFly proporciona los fixtures `postgres_container` y `redis_container` que cablean Testcontainers en el contenedor de inyección de dependencias mediante una configuración al estilo `@ServiceConnection` (Capítulo 16).

**Objeto de valor (value object)** — Un objeto de dominio inmutable identificado por su valor en lugar de por un campo de identidad. Dos objetos de valor son iguales si todos sus campos son iguales. En PyFly, `ValueObject` es la clase base; aplica `@dataclass(frozen=True)` para imponer la inmutabilidad. `Money` —un importe entero en **unidades menores** (céntimos) y un `Currency` `StrEnum`— es el objeto de valor canónico de Lumen; `Wallet` es la raíz de agregado que lo posee (Capítulo 6).

**Webhook** — Una llamada de retorno HTTP entrante que un proveedor externo (Stripe, Twilio, etc.) invoca para notificar a tu servicio de un evento asíncrono, como un cambio en el estado de un pago. El decorador `@webhook_listener` de PyFly verifica la firma HMAC-SHA256, deduplica las repeticiones mediante una caché de nonces y enruta la carga útil a un manejador tipado (Capítulo 17).

**Flujo de trabajo (workflow)** — Una variante de larga duración del patrón saga en la que los pasos pueden pausarse durante minutos, horas o a la espera de aprobación humana antes de reanudarse. Los flujos de trabajo persisten su estado entre pasos para sobrevivir a los reinicios del proceso; los decoradores `@workflow` y `@workflow_step` de PyFly proporcionan esta capacidad sobre el mismo motor de sagas (Capítulo 12).
