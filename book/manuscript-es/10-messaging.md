<span class="eyebrow">Capítulo 10</span>

# Mensajería con Kafka y RabbitMQ {.chtitle}

::: figure art/openers/ch10.svg | &nbsp;

El servicio de monederos de Lumen es genuinamente orientado a eventos. Los comandos fluyen a través de manejadores tipados; los eventos de dominio se publican en un bus en proceso; los oyentes reaccionan de forma independiente sin acoplarse a la ruta de escritura. En el Capítulo 8 construiste `WalletAuditListener`, un servicio que reacciona a los eventos `WalletOpened`, `FundsDeposited` y `FundsWithdrawn` sin conocer los manejadores de comandos. En el Capítulo 9 fuiste más allá, almacenando esos eventos *como la fuente de la verdad* para que cada saldo histórico se pueda calcular desde primeros principios.

Hay un límite que ninguno de los dos capítulos cruzó: la red. `InMemoryEventBus` vive dentro del proceso de Python. En el momento en que otro servicio —un futuro `PaymentsService` que liquide transferencias, o un `NotificationsService` que envíe alertas push— necesite reaccionar a los hechos de Lumen, necesitas un **broker de mensajería** (message broker): un componente de infraestructura independiente que almacena eventos de forma duradera, los enruta a suscriptores en otros procesos y los reproduce cuando un consumidor se reinicia tras una caída.

Este capítulo lleva la base orientada a eventos de Lumen al otro lado de ese límite. Verás cómo PyFly envuelve la complejidad de Apache Kafka y RabbitMQ tras una única abstracción limpia —`MessageBrokerPort`— de modo que el código de aplicación nunca sepa qué broker se está ejecutando debajo. Publicarás los eventos de monedero de Lumen en topics reales, los consumirás con el decorador `@message_listener`, elegirás el formato de serialización adecuado para tus requisitos de evolución de esquema, gestionarás los mensajes envenenados con colas de mensajes muertos integradas en el decorador y protegerás tu servicio frente a caídas del broker con cortacircuitos (circuit breakers) y reintentos.

Al final del capítulo, los eventos de integración de Lumen fluyen a través de los límites de proceso, listos para los servicios de la Parte IV que los consumirán.

Lo construiremos gradualmente, una pieza cada vez. Cada funcionalidad viene con un recorrido numerado, el comando exacto que ejecutar y la salida que deberías esperar ver. Si has seguido el hilo desde el Capítulo 8, ya tienes `EventPublisher` conectado a los manejadores de comandos del monedero y el `WalletAuditListener` reaccionando en proceso; este capítulo está verificado contra PyFly v26.6.110 y el ejemplo de Lumen ubicado en `samples/lumen`. Nada de lo que hay aquí requiere un clúster de Kafka o RabbitMQ en ejecución para seguir adelante: PyFly incluye un broker en memoria que satisface el mismo contrato, de modo que puedes leer, ejecutar y probar cada listado antes de tocar siquiera Docker.

!!! note "La jerga, en lenguaje sencillo"
    Un puñado de términos se repiten en este capítulo. Un **broker de mensajería** es un servidor independiente (Kafka o RabbitMQ) que almacena mensajes y los entrega a otros procesos. Un **topic** es un canal con nombre en el broker; los publicadores escriben en él y los suscriptores leen de él. Un **productor** (o *publicador*) coloca mensajes en un topic; un **consumidor** (u *oyente*) los retira y reacciona. Un **grupo de consumidores** es una etiqueta que permite que varias copias del mismo servicio compartan el trabajo, de modo que cada mensaje se gestione una sola vez. Un **serializador** convierte un objeto de Python en los `bytes` en bruto que el broker almacena; un **deserializador** convierte esos bytes de nuevo en un objeto en el otro lado. Una **cola de mensajes muertos** (DLQ) es un área de retención para mensajes que no pudieron procesarse. Un **adaptador** es el controlador concreto del broker que se oculta tras la interfaz `MessageBrokerPort`. Ten presentes estos ocho; el resto del capítulo trata, sobre todo, de conectarlos.

---

## Una abstracción, muchos brokers

### Por qué importa una abstracción

Antes de escribir una línea de código de Kafka o RabbitMQ, vale la pena preguntarse: ¿por qué introduce PyFly una capa de abstracción en absoluto? Tanto `aiokafka` como `aio-pika` exponen APIs asíncronas perfectamente usables. La respuesta es la misma razón por la que dependes de `EventPublisher` en lugar de `InMemoryEventBus`: la abstracción es lo que te permite intercambiar infraestructura sin tocar la lógica de negocio.

Sin una abstracción, cada servicio que produce o consume un mensaje importa tipos específicos de Kafka o de RabbitMQ. Cambiar de broker —o ejecutar Kafka en producción y un broker en memoria en CI— significa cambiar rutas de importación, firmas de constructores y código repetitivo de bucles de consumo en cada archivo afectado. Con `MessageBrokerPort`, el cambio es una modificación de YAML. Los oyentes y publicadores que componen tu lógica de negocio nunca cambian.

La abstracción también da frutos en las pruebas. `InMemoryMessageBroker` satisface el protocolo del port. Inyéctalo allí donde se espera un `MessageBrokerPort` y escribe pruebas rápidas y deterministas sin dependencia de Docker. El Capítulo 16 lo concreta.

### El protocolo MessageBrokerPort

**`MessageBrokerPort`** es un `@runtime_checkable Protocol`. Úsalo como anotación de tipo en todo tu código; llama a `isinstance(obj, MessageBrokerPort)` en tiempo de ejecución si necesitas verificar que un bean inyectado satisface el contrato.

El protocolo define cuatro métodos:

```python
from pyfly.messaging import MessageBrokerPort

class MessageBrokerPort(Protocol):
    async def publish(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        group: str | None = None,
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
```

**Los cuatro métodos:**

`publish` envía un único mensaje al topic con nombre. `value` son bytes en bruto: el protocolo deja la serialización deliberadamente en tus manos; codifica la carga útil antes de llamar a `publish` y decodifícala dentro de tu manejador. `key` y `headers` son de tipo keyword-only, de modo que quien llama no pueda transponerlos por accidente. `key` dirige la asignación de particiones de Kafka para las garantías de orden; RabbitMQ lo ignora. `headers` transportan metadatos transversales como `event-type` e identificadores de correlación.

`subscribe` registra un `MessageHandler` asíncrono para un topic. El parámetro opcional `group` se corresponde con los grupos de consumidores de Kafka y las colas de consumidores en competencia de RabbitMQ. Despliega tres instancias de un servicio, todas suscribiéndose con el mismo `group`, y solo una instancia procesa cada mensaje. Omite `group` para una semántica de difusión —cada suscriptor recibe cada mensaje—, lo cual resulta útil para analíticas que necesitan una copia de cada evento.

`start` crea las conexiones y comienza a consumir. Registra todas las suscripciones *antes* de llamar a `start`, y luego llámalo una sola vez durante el arranque de la aplicación.

`stop` drena los mensajes en vuelo y cierra las conexiones de forma limpia. El ciclo de vida de la aplicación de PyFly llama a `stop` automáticamente durante el apagado, así que rara vez necesitarás invocarlo a mano.

### La dataclass Message

Cada manejador recibe un **`Message`**: una dataclass congelada que transporta el sobre completo de un mensaje recibido:

```python
from pyfly.messaging import Message

msg = Message(
    topic="wallet.events",
    value=b'{"wallet_id": "w-001", "amount": 5000}',
    key=b"w-001",
    headers={"event-type": "FundsDeposited"},
)
```

| Campo | Tipo | Valor por defecto | Descripción |
|---|---|---|---|
| `topic` | `str` | obligatorio | El topic o cola en el que llegó el mensaje. |
| `value` | `bytes` | obligatorio | La carga útil en bruto. La decodificas dentro de tu manejador. |
| `key` | `bytes \| None` | `None` | Clave de partición o enrutamiento. Kafka la usa para la asignación de particiones. |
| `headers` | `dict[str, str]` | `{}` | Metadatos de cadena adjuntados por el publicador. |

La dataclass está congelada: una vez que el broker te entrega un `Message`, sus campos son inmutables, seguros para pasar a través de límites asíncronos sin copia defensiva e inmunes a la mutación accidental dentro de los manejadores.

### Kafka frente a RabbitMQ: elegir el broker adecuado

Antes de sumergirnos en la configuración, ayuda entender dónde encaja cada broker. La tabla siguiente resume las concesiones clave; ninguna de las dos opciones es universalmente correcta.

::: figure art/figures/10-messaging.svg | Figura 10.1 — MessageBrokerPort se sitúa entre el código de aplicación y los adaptadores de broker.

| Dimensión | Apache Kafka | RabbitMQ |
|---|---|---|
| **Modelo** | Registro de confirmación (commit log) distribuido; los consumidores mantienen su propio offset | Broker de mensajería; los mensajes se eliminan de la cola tras la confirmación |
| **Retención** | Configurable (de días a indefinida); los consumidores pueden reproducir desde cualquier offset | Los mensajes se eliminan al entregarse; colas de mensajes muertos para los fallidos |
| **Rendimiento** | Millones de mensajes/segundo; optimizado para streaming | Decenas de miles/segundo; optimizado para enrutamiento de tareas |
| **Orden** | Garantizado dentro de una partición (productores con clave) | Garantizado dentro de una sola cola (FIFO) |
| **Grupos de consumidores** | Balanceo de carga nativo a nivel de partición | Colas de consumidores en competencia; un mensaje por consumidor |
| **Evolución de esquema** | Funciona bien con Avro/Protobuf + Schema Registry | Funciona bien con JSON; el acoplamiento de esquema es responsabilidad del usuario |
| **Cuándo elegirlo** | Streaming de eventos, registros de auditoría, reproducción, alto rendimiento | Colas de tareas, patrones RPC, enrutamiento por mensaje con bindings complejos |
| **Extra de PyFly** | `uv add "pyfly[kafka]"` | `uv add "pyfly[rabbitmq]"` |

Para Lumen, Kafka es el ajuste natural: los eventos de monedero forman un flujo ordenado por monedero, merece la pena reproducirlos cuando un nuevo consumidor se conecta y, con el tiempo, alimentarán analíticas de alto rendimiento. Los ejemplos de este capítulo muestran ambos adaptadores de forma intercambiable: desde la perspectiva de tu código, la elección es un detalle de configuración.

!!! note "Instalar ambos adaptadores"
    Si quieres dar soporte a cualquiera de los dos brokers en una sola
    instalación, `uv add "pyfly[eda]"` incorpora tanto `aiokafka` como
    `aio-pika`. La autoconfiguración selecciona entonces Kafka si `aiokafka`
    es importable, RabbitMQ si `aio_pika` es importable, y recurre al broker
    en memoria si ninguno está presente.

---

## Configurar los adaptadores

Conectar un broker a Lumen es una tarea de configuración, no de codificación. Añades un extra al proyecto, agregas un bloque `pyfly.messaging` a `pyfly.yaml` y PyFly hace el resto: construye el adaptador correcto y lo registra bajo el bean `MessageBrokerPort`, de modo que cualquier cosa que pida ese port reciba inyectado el broker en ejecución. Empezaremos con el broker que no necesita infraestructura alguna y luego pasaremos a Kafka y RabbitMQ.

!!! note "Activa la mensajería con una sola clave"
    En la v26.6.110, el subsistema de mensajería solo se conecta por sí mismo
    cuando la clave `pyfly.messaging.provider` está **presente** en tu
    configuración. Sin clave no hay bean `MessageBrokerPort`: un deliberado
    "desactivado por defecto" para que una app sin mensajería no necesite
    incorporar ninguna biblioteca de broker. Una vez establecida la clave, su
    valor (`"memory"`, `"kafka"`, `"rabbitmq"` o `"auto"`) selecciona el
    adaptador. Las claves complementarias viven bajo el mismo bloque:
    `pyfly.messaging.kafka.bootstrap-servers` y
    `pyfly.messaging.rabbitmq.url`.

### Empieza con el broker en memoria

El `pyfly.yaml` de Lumen ya se ejecuta sobre el bus EDA en memoria del Capítulo 8. Para poner en marcha la abstracción de *mensajería* sin levantar Docker, añade una única línea `provider`.

**Paso 1 — Habilitar el broker en memoria.** Abre `pyfly.yaml` y añade un bloque `messaging` bajo `pyfly`:

```yaml
pyfly:
  messaging:
    provider: "memory"
```

**Paso 2 — Añadir un oyente para que haya algo que despertar.** Coloca el oyente independiente del Listado 10.4 (unas páginas más adelante) en `src/lumen/messaging/payments_consumer.py`. En el arranque, PyFly descubre la función marcada y la suscribe por ti: no escribes ninguna llamada a `subscribe()`.

!!! tip "Ejecútalo"
    Arranca la app. El broker en memoria no necesita ningún servidor externo,
    así que esto funciona en un portátil sin nada instalado:

    ```bash
    uv run pyfly run
    ```

    El banner de arranque informa de la versión del framework y el puerto
    enlazado (`pyfly.server.port`, `8080` por defecto en la v26.6.110):

    ```
    :: PyFly Framework :: (v26.06.110) (Python 3.13.13)
    app=lumen version=1.0.0 ... started_in=0.42s port=8080
    ```

    No se imprime nada más todavía: no se ha publicado ningún mensaje. Eso es
    lo esperado: el broker está en ejecución y el oyente está suscrito,
    esperando. Las secciones siguientes le darán eventos que transportar.

**Qué acaba de ocurrir.** Una línea de YAML cambió el bean `MessageBrokerPort` de "no presente" a un broker en memoria funcional, y el framework autosuscribió tu `@message_listener` a él durante el arranque. Sin Kafka, sin RabbitMQ, sin Docker; y, sin embargo, *exactamente el mismo código* se ejecutará contra un broker real en cuanto cambies esa única línea a `"kafka"`. Esa propiedad de cambiar-sin-recompilar es todo el sentido de la abstracción.

### Kafka

Cuando estés listo para un broker real, añade `pyfly[kafka]` a tu proyecto y apunta el provider a Kafka.

**Paso 1 — Instalar el extra de Kafka.** Esto incorpora `aiokafka`, el controlador asíncrono que envuelve el `KafkaAdapter` de PyFly:

```bash
uv add "pyfly[kafka]"
```

**Paso 2 — Declarar el broker en `pyfly.yaml`.** Cambia el provider y enumera tus brokers:

```yaml
pyfly:
  messaging:
    provider: "kafka"
    kafka:
      bootstrap-servers: "kafka-1:9092,kafka-2:9092"
```

Eso es todo lo que PyFly necesita para autoconfigurar un `KafkaAdapter` y registrarlo como el bean `MessageBrokerPort`. (`bootstrap-servers` es una lista separada por comas de pares `host:port` —las direcciones de uno o más brokers del clúster—; el cliente descubre el resto a partir de cualquiera de ellos.) Para la mayoría de los servicios el YAML es suficiente; si necesitas opciones avanzadas de productor, construye el adaptador manualmente como un `@bean` dentro de una clase `@configuration`.

### RabbitMQ

```yaml
pyfly:
  messaging:
    provider: "rabbitmq"
    rabbitmq:
      url: "amqp://user:password@rabbitmq-host:5672/"
```

`RabbitMQAdapter` usa por defecto un exchange directo duradero llamado `"pyfly"`. Para personalizar el nombre del exchange, construye el adaptador manualmente:

::: listing lumen/messaging/config.py | Listado 10.1 — Nombre de exchange de RabbitMQ personalizado mediante @bean
from pyfly.container import configuration, bean
from pyfly.messaging import MessageBrokerPort
from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter


@configuration
class BrokerConfig:
    """Wire up the message broker bean."""

    @bean
    def broker(self) -> MessageBrokerPort:
        return RabbitMQAdapter(
            url="amqp://user:password@rabbitmq-host:5672/",
            exchange_name="lumen-events",
        )
:::

**Cómo funciona.** `@configuration` marca la clase como una fábrica que el contenedor de DI llama durante el arranque. `@bean` sobre `broker` le indica al contenedor que llame a `broker()` una vez, almacene en caché el resultado y lo inyecte allí donde se solicite `MessageBrokerPort`. Cualquier `@service` que declare `MessageBrokerPort` en su constructor recibe esta instancia automáticamente, sin necesidad de importar `RabbitMQAdapter` en la clase consumidora.

### Autodetección

Cuando `provider` es `"auto"`, PyFly sondea los paquetes instalados en orden y elige el primer broker que encuentra:

| Prioridad | Biblioteca comprobada | Adaptador seleccionado |
|---|---|---|
| 1 | `aiokafka` | `KafkaAdapter` |
| 2 | `aio_pika` | `RabbitMQAdapter` |
| 3 | *(reserva)* | `InMemoryMessageBroker` |

`provider: "memory"` es distinto de `"auto"`: *siempre* selecciona el broker en memoria con independencia de lo que esté instalado, que es exactamente lo que quieres en las pruebas. Un `provider: "kafka"` o `"rabbitmq"` explícito omite el sondeo por completo y exige que la biblioteca de ese adaptador esté presente.

El patrón práctico es un YAML por entorno. Establece `provider: "memory"` en `pyfly-test.yaml` y `provider: "kafka"` en `pyfly-prod.yaml`, y cada ejecución de prueba y de producción usará el adaptador apropiado sin cambios de código.

!!! tip "Ejecútalo"
    Puedes confirmar qué adaptador seleccionó PyFly sin enviar un solo
    mensaje. Con la mensajería habilitada, arranca la app y busca la línea del
    broker en el registro de arranque:

    ```bash
    uv run pyfly run
    ```

    ```
    pyfly.messaging  provider=memory broker=InMemoryMessageBroker started
    ```

    Cambia `provider` a `"kafka"` (con `pyfly[kafka]` instalado y un broker
    accesible) y reinicia; la misma línea informa ahora de
    `broker=KafkaAdapter`. El código de negocio que publica y consume no
    cambió: solo lo hizo el YAML.

---

## Publicar eventos de integración

### De eventos en proceso a eventos de integración

En el Capítulo 8, los manejadores de comandos de Lumen drenaban el búfer de eventos de la raíz de agregado `Wallet` con `wallet.clear_events()` y publicaban cada evento de dominio a través de `EventPublisher`. `WalletAuditListener` se suscribía usando `@event_listener` y reaccionaba dentro del mismo proceso.

El patrón de **evento de integración** cruza el límite de proceso. Mientras que un *evento de dominio* describe lo que ocurrió dentro de un agregado —un hecho privado, disponible para los oyentes del mismo proceso—, un evento de integración es una representación pública y depurada del mismo hecho: diseñada para consumidores externos, estable entre versiones y serializada a bytes para su transporte por un broker.

Para Lumen, el evento de integración de un depósito transporta solo lo que un consumidor externo necesita: el identificador del monedero, el importe en unidades menores, el código de moneda y el saldo resultante. No expone los detalles internos de implementación del agregado.

### Cómo drena Lumen los eventos hacia el broker

Lumen separa el puente de publicación de los manejadores de comandos, de modo que cada manejador publique eventos de forma idéntica. `publish_domain_events` (en `lumen/core/services/wallets/event_publishing.py`) itera los eventos drenados, convierte cada dataclass congelada en un dict y llama a `EventPublisher.publish`:

```python
# lumen/core/services/wallets/event_publishing.py  (real Lumen code)
from pyfly.eda import EventPublisher
from pyfly.domain import DomainEvent

async def publish_domain_events(
    publisher: EventPublisher,
    events: Iterable[DomainEvent],
) -> None:
    for event in events:
        payload = dataclasses.asdict(event)
        payload.setdefault("event_type", event.event_type)
        await publisher.publish(
            destination="wallet.events",
            event_type=event.event_type,   # "WalletOpened" / "FundsDeposited" / …
            payload=payload,
        )
```

La firma de `EventPublisher.publish` es:

```python
async def publish(
    self,
    destination: str,
    event_type: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> None: ...
```

`destination` es el nombre lógico del canal (`"wallet.events"`). `event_type` es el nombre de la clase del evento de dominio —`"WalletOpened"`, `"FundsDeposited"` o `"FundsWithdrawn"`—, que es exactamente lo que filtran los suscriptores `@event_listener`.

Cada manejador de comandos conecta `EventPublisher` mediante el constructor y llama a `publish_domain_events` tras persistir:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 10.2 — DepositFundsHandler drena eventos mediante EventPublisher
from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(self, command: DepositFunds) -> int:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.deposit(Money(
            amount=command.amount,     # integer minor units (e.g. 5000 = €50.00)
            currency=wallet.currency,
        ))
        await self._repository.add(wallet)

        # Drain pending events and forward them to the EDA bus.
        await publish_domain_events(
            self._events, wallet.clear_events()
        )
        return wallet.balance.amount
:::

**Decisiones de diseño clave:**

`events: EventPublisher` es el port, no el adaptador. El contenedor de DI inyecta el bus que esté configurado —en memoria en las pruebas, un bus respaldado por broker en producción—. Este manejador nunca menciona Kafka ni RabbitMQ.

`command.amount` es el depósito en unidades menores enteras (p. ej., `5000` significa 50,00 € para un monedero en EUR). El evento de dominio `FundsDeposited` registra el mismo campo `amount`, más `currency` (una cadena como `"EUR"`) y `balance` (el nuevo saldo en unidades menores).

`wallet.clear_events()` drena la lista de eventos pendientes del agregado y los devuelve. Llamarlo *después* de `repository.add` garantiza que los eventos describan un hecho que persistió. Publicar antes de guardar crearía eventos fantasma: hechos sobre cosas que nunca ocurrieron.

Los eventos de dominio emitidos durante un depósito son instancias de:

```python
@dataclass(frozen=True)
class FundsDeposited(DomainEvent):
    wallet_id: str = ""
    amount: int = 0      # integer minor units
    currency: str = ""   # e.g. "EUR"
    balance: int = 0     # new balance after deposit, minor units
```

Cuando `publish_domain_events` publica este evento, `event_type` es el nombre de la clase `"FundsDeposited"`, *no* una cadena con puntos como `"wallet.fundsdeposited"`.

### Publicar un evento de integración directamente en el broker

Cuando un servicio independiente que se ejecuta en un proceso distinto necesita recibir los eventos de monedero de Lumen, el bus EDA debe estar respaldado por un adaptador de broker real. La carga útil que circula por el cable es el mismo dict que ven los oyentes en proceso. Un `OutboxRelay` dedicado (tratado en la sección de resiliencia) o un `EventPublisher` respaldado por broker se encargan del transporte.

Ayuda ver la publicación primero en su forma más pequeña posible. El siguiente listado es una sencilla función `async` —sin clase, sin decorador— que toma un `MessageBrokerPort`, construye la carga útil y llama a `publish`. Constrúyela en tres movimientos:

**Paso 1 — Codificar la carga útil a bytes.** `MessageBrokerPort.publish` solo ve `bytes`, así que la función serializa el evento con `json.dumps(...).encode()`. El `.encode()` convierte la cadena JSON en bytes UTF-8 que el broker puede almacenar literalmente.

**Paso 2 — Elegir una clave de partición.** Pasar `key=wallet_id.encode()` le indica a Kafka que enrute cada mensaje de un monedero dado a la misma partición, lo que preserva su orden. (RabbitMQ ignora la clave, así que incluirla es inofensivo en cualquier caso.)

**Paso 3 — Adjuntar la cabecera de tipo de evento.** `headers={"event-type": "FundsDeposited"}` permite a un consumidor decidir si le interesa este mensaje *antes* de deserializar el cuerpo: enrutamiento barato.

::: listing lumen/messaging/deposit_publisher.py | Listado 10.3 — Publicar un evento de integración de monedero en un topic de Kafka
from __future__ import annotations

import json

from pyfly.messaging import MessageBrokerPort


async def publish_deposit_event(
    broker: MessageBrokerPort,
    wallet_id: str,
    amount: int,
    currency: str,
    balance: int,
) -> None:
    """Encode a FundsDeposited integration event and publish to the topic."""
    payload = json.dumps({
        "wallet_id": wallet_id,
        "amount": amount,        # integer minor units
        "currency": currency,    # e.g. "EUR"
        "balance": balance,      # new balance, minor units
        "event_type": "FundsDeposited",
    }).encode()

    await broker.publish(
        "wallet.events",
        payload,
        key=wallet_id.encode(),
        headers={"event-type": "FundsDeposited"},
    )
:::

**Decisiones de diseño clave:**

`broker: MessageBrokerPort` es el port, no el adaptador. El contenedor de DI inyecta el adaptador que esté configurado —Kafka en producción, el broker en memoria en las pruebas—.

`key=wallet_id.encode()` es la clave de enrutamiento. En Kafka, todos los mensajes que comparten la misma clave aterrizan en la misma partición, entregándolos a los consumidores en orden de publicación: crítico para un libro mayor donde el depósito antes de la retirada debe preservarse. En RabbitMQ la clave se ignora (el enrutamiento usa el binding del exchange), así que este campo es seguro de incluir con independencia del broker en ejecución.

`headers={"event-type": "FundsDeposited"}` usa el nombre de la clase del evento de dominio, no una ruta con puntos como `"wallet.fundsdeposited"`. Los consumidores pueden inspeccionar el tipo de evento sin decodificar la carga útil, lo cual es útil para el enrutamiento y el filtrado sin una deserialización completa.

**Qué acaba de ocurrir.** Cruzaste el límite de proceso. El mismo hecho `FundsDeposited` que `WalletAuditListener` consumió en proceso en el Capítulo 8 son ahora bytes en un topic, direccionables por cualquier servicio que se conecte al broker; y la función que los colocó ahí no nombra ningún broker, solo el port `MessageBrokerPort`. Intercambia el adaptador configurado y este código permanece sin cambios.

!!! warning "Publica después de guardar, no antes"
    Drena y publica siempre los eventos *después* de
    `repository.add(wallet)`. Si el guardado falla, ningún mensaje llega al
    broker y los consumidores externos nunca ven un hecho que nunca persistió.
    El patrón de outbox transaccional (donde la fila del outbox y la fila del
    agregado se escriben en la misma transacción de base de datos) ofrece la
    garantía atómica más fuerte para producción; la publicación directa que se
    muestra aquí es un punto de partida razonable para servicios más simples.

---

## Consumir eventos con @message_listener

### El problema del sondeo

Antes de los brokers, los servicios reaccionaban a los cambios de estado de otro servicio sondeando una base de datos compartida o un endpoint REST. El sondeo añade latencia (la reacción espera hasta el siguiente intervalo de sondeo), desperdicia recursos (la mayoría de los sondeos no encuentran nada nuevo) y acopla el consumidor al productor a nivel de API. Un oyente de mensajes elimina los tres problemas: el broker empuja el evento en cuanto está disponible, las conexiones inactivas consumen una CPU insignificante y el consumidor depende solo del esquema del mensaje, no de la API interna del productor.

### Oyentes declarativos con @message_listener

**`@message_listener`** es el decorador de suscripción declarativa. Decora cualquier función o método asíncrono con el topic que debe consumir, y PyFly conecta la suscripción durante el arranque de la aplicación: sin referencia al bus, sin llamada a `subscribe()`, sin gestión del ciclo de vida en tu código.

La firma del decorador es:

```python
def message_listener(
    topic: str,
    group: str | None = None,
    *,
    retries: int = 0,
    retry_delay: float = 0.0,
    dead_letter_topic: str | None = None,
) -> ...: ...
```

| Parámetro | Tipo | Valor por defecto | Descripción |
|---|---|---|---|
| `topic` | `str` | obligatorio | El topic en el que escuchar. |
| `group` | `str \| None` | `None` | Nombre del grupo de consumidores. |
| `retries` | `int` | `0` | Veces que reinvocar el manejador ante un fallo. |
| `retry_delay` | `float` | `0.0` | Retardo base (segundos) entre reintentos: el intento N espera `retry_delay * N`. |
| `dead_letter_topic` | `str \| None` | `None` | Cuando se establece, un mensaje que sigue fallando tras `retries` se republica aquí. |

El primer oyente es una función independiente, la forma más simple. Constrúyela en tres movimientos:

**Paso 1 — Escribir una función asíncrona que tome un `Message`.** Cada oyente recibe un argumento: el sobre congelado `Message` (`topic`, `value`, `key`, `headers`). La función debe ser `async` porque el broker la espera (await).

**Paso 2 — Decorarla con el topic y el grupo.** `@message_listener(topic="wallet.events", group="payments-service")` es la suscripción entera. No hay ninguna llamada a `subscribe()` que escribir ni ningún bus que importar: el decorador marca la función con metadatos que el framework lee en el arranque.

**Paso 3 — Decodificar dentro del manejador.** El cuerpo comprueba la cabecera `event-type` y luego `json.loads(msg.value)` convierte los bytes en bruto de nuevo en un dict. El manejador decide qué le importa; aquí reacciona solo a `FundsDeposited`.

::: listing lumen/messaging/payments_consumer.py | Listado 10.4 — @message_listener sobre una función independiente
from __future__ import annotations

import json

from pyfly.messaging import Message, message_listener


@message_listener(topic="wallet.events", group="payments-service")
async def on_wallet_event(msg: Message) -> None:
    """React to every wallet event published to the topic."""
    event_type = msg.headers.get("event-type", "unknown")
    payload = json.loads(msg.value)

    if event_type == "FundsDeposited":
        wallet_id: str = payload["wallet_id"]
        amount: int = payload["amount"]        # minor units
        currency: str = payload["currency"]
        print(
            f"[Payments] Deposit received: "
            f"wallet={wallet_id} "
            f"amount={amount} {currency}"
        )
:::

**Cómo funciona.** El decorador almacena seis atributos de metadatos en la función envuelta —`__pyfly_message_listener__ = True`, más `__pyfly_listener_topic__`, `__pyfly_listener_group__`, `__pyfly_listener_retries__`, `__pyfly_listener_retry_delay__` y `__pyfly_listener_dlq__`—. Durante el arranque, el framework escanea todos los beans registrados, encuentra las funciones que llevan `__pyfly_message_listener__ = True` y llama a `broker.subscribe(topic, handler, group)` automáticamente. Nunca llamas a `subscribe()` a mano.

`group="payments-service"` coloca al consumidor en un grupo de consumidores. Escala a múltiples instancias del servicio de pagos y solo una procesa cada mensaje: el broker distribuye la carga entre el grupo. Omite `group` para una semántica de difusión en la que cada instancia recibe cada mensaje.

Dentro del manejador, `msg.headers.get("event-type", "unknown")` inspecciona los metadatos del sobre antes de tocar la carga útil. El valor de la cabecera es el nombre de la clase del evento de dominio —`"FundsDeposited"`, `"WalletOpened"` o `"FundsWithdrawn"`—, coincidiendo con lo que Lumen establece en el lado del publicador.

!!! tip "Ejecútalo"
    Con `provider: "memory"` establecido (sin Docker), este es el viaje de
    ida y vuelta completo publicar → consumir en un solo lugar. Guarda el
    fragmento de abajo como `roundtrip.py` y ejecútalo con
    `uv run python roundtrip.py`:

    ```python
    import asyncio, json
    from pyfly.messaging.adapters.memory import InMemoryMessageBroker
    from lumen.messaging.payments_consumer import on_wallet_event

    async def main() -> None:
        broker = InMemoryMessageBroker()
        await broker.subscribe(
            "wallet.events", on_wallet_event, group="payments-service"
        )
        await broker.start()
        await broker.publish(
            "wallet.events",
            json.dumps({
                "wallet_id": "w-001", "amount": 5000,
                "currency": "EUR", "balance": 5000,
            }).encode(),
            headers={"event-type": "FundsDeposited"},
        )
        await asyncio.sleep(0.1)   # let the listener run
        await broker.stop()

    asyncio.run(main())
    ```

    El oyente imprime la línea que construyó a partir de la carga útil
    decodificada:

    ```
    [Payments] Deposit received: wallet=w-001 amount=5000 EUR
    ```

    Dentro de una app en ejecución *no* escribirías este cableado a mano: el
    decorador `@message_listener` y el bean del broker configurado hacen el
    `subscribe`/`start`/`stop` por ti. Este script independiente solo hace
    visible el viaje de ida y vuelta de forma aislada.

**Qué acaba de ocurrir.** Un mensaje que publicaste en `wallet.events` llegó a una función que nunca conectaste explícitamente a nada. El decorador transportó el topic y el grupo; el broker (aquí en memoria, en producción Kafka) hizo la entrega. Ese es el lado de consumo de la misma abstracción que usaste para publicar, y el cuerpo de la función es agnóstico respecto al broker de arriba abajo.

### Oyentes en clases de servicio

Cuando un oyente necesita colaboradores —un repositorio, otro servicio—, declíralo como un método en una clase `@service`. PyFly inyecta las dependencias a través del constructor y conecta la suscripción del oyente después de que el bean se inicialice. La forma cambia solo ligeramente respecto a la versión independiente:

**Paso 1 — Convertir la clase en un `@service`.** Esto la registra en el contenedor de DI para que el framework pueda tanto inyectar su constructor como descubrir su método oyente.

**Paso 2 — Declarar los colaboradores en el constructor.** Aquí `smtp_client` representa un servicio de correo electrónico o push; el contenedor lo suministra. La función libre del Listado 10.4 no tenía dónde guardar tal dependencia: esa es la razón para recurrir a una clase.

**Paso 3 — Decorar un *método* con `@message_listener`.** La firma gana `self`, pero por lo demás el decorador y el cuerpo son idénticos a la forma de función. Como el bean se crea primero, `self._smtp` está listo para cuando llega un mensaje.

::: listing lumen/messaging/notifications_consumer.py | Listado 10.5 — @message_listener sobre un método de @service con dependencias
from __future__ import annotations

import json

from pyfly.container import service
from pyfly.messaging import Message, message_listener


@service
class WalletNotificationConsumer:
    """Sends push notifications when wallet events arrive via the broker."""

    def __init__(self, smtp_client: object) -> None:
        # smtp_client would be an injected email/push service.
        self._smtp = smtp_client

    @message_listener(topic="wallet.events", group="notifications-service")
    async def on_wallet_event(self, msg: Message) -> None:
        event_type = msg.headers.get("event-type", "unknown")

        if event_type != "WalletOpened":
            return

        payload = json.loads(msg.value)
        owner_id: str = payload.get("owner_id", "")
        wallet_id: str = payload.get("wallet_id", "")
        currency: str = payload.get("currency", "")

        print(
            f"[Notification] Welcome {owner_id}! "
            f"Your {currency} wallet {wallet_id} is ready."
        )
:::

**Cómo funciona.** `@service` registra `WalletNotificationConsumer` en el contenedor de DI. El constructor recibe `smtp_client` mediante inyección. Después de crear el bean, el framework detecta `on_wallet_event` llevando `__pyfly_message_listener__ = True` y lo registra como un oyente de método enlazado (bound-method): `self` ya está capturado, así que cada invocación tiene acceso completo a `self._smtp`.

El retorno temprano cuando `event_type != "WalletOpened"` es una guarda de filtrado. Un único topic (`wallet.events`) transporta múltiples tipos de evento, así que cada oyente filtra los que le interesan. Esto es más simple que mantener un topic separado por tipo de evento, aunque para flujos de muy alto volumen, un topic-por-tipo es una concesión de diseño legítima.

!!! tip "Semántica de grupos de consumidores de un vistazo"
    Dos servicios con nombres de grupo *distintos* reciben cada uno cada
    mensaje: el broker entrega una copia a cada grupo. Dos *instancias* del
    mismo servicio que comparten el *mismo* nombre de grupo comparten la
    carga: cada mensaje va exactamente a una instancia. Usa grupos distintos
    para fanout (pagos y notificaciones necesitan ambos el evento); usa el
    mismo grupo para escalado horizontal (tres instancias del servicio de
    pagos comparten el trabajo).

---

## Serialización y evolución de esquema

### Por qué bytes, y por qué importa

`MessageBrokerPort.publish` acepta `bytes` en bruto. Esa es una elección deliberada. Un adaptador de broker que forzara un único formato de serialización sería cómodo para los casos simples y doloroso para todo lo demás: la evolución de esquema, los consumidores multilenguaje, los requisitos de cumplimiento y las restricciones de rendimiento empujan en direcciones distintas. Al dejar la serialización en tus manos, PyFly se mantiene al margen.

Vale la pena conocer tres formatos: JSON por su simplicidad, Avro para la evolución respaldada por un registro de esquemas, y Protobuf para entornos críticos en rendimiento o multilenguaje:

| Formato | Legible por humanos | Cumplimiento de esquema | Evolución de esquema | Multilenguaje | Codificación en PyFly |
|---|---|---|---|---|---|
| **JSON** | Sí | Opcional | Manual (disciplina del consumidor) | Universal | `json.dumps(...).encode()` |
| **Avro** | No | Sí (vía registro) | De primera clase (`BACKWARD` / `FORWARD` / `FULL`) | Buena | Biblioteca `fastavro` |
| **Protobuf** | No | Sí (archivos `.proto`) | De primera clase (numeración de campos) | Excelente | Biblioteca `protobuf` |

### JSON: empieza aquí

La *serialización* es solo el acto de convertir un objeto en memoria en una secuencia plana de bytes que puedes almacenar o enviar, y la *deserialización* es lo inverso. Los tres formatos siguientes difieren únicamente en cuán compactos son esos bytes y con cuánta rigurosidad vigilan la forma de los datos.

JSON es el valor por defecto adecuado. No requiere herramientas más allá de la biblioteca estándar, cualquier lenguaje puede analizarlo y la carga útil es legible en las interfaces de monitorización del broker. El patrón de codificación son dos líneas:

```python
import json

payload: bytes = json.dumps({
    "wallet_id": "w-001",
    "amount": 5000,          # integer minor units (€50.00)
    "currency": "EUR",
    "balance": 10000,        # new balance, minor units
    "event_type": "FundsDeposited",
}).encode()

await broker.publish("wallet.events", payload)
```

Decodificar en el consumidor:

```python
data: dict = json.loads(msg.value)
```

La debilidad de JSON es que el esquema no se hace cumplir. Si un publicador añade un campo obligatorio y el consumidor no se ha actualizado, el consumidor se rompe silenciosamente. Para los eventos internos de Lumen, donde productor y consumidor se despliegan juntos, esto es manejable. Para los eventos compartidos con equipos externos o los topics de larga vida, necesitas garantías más fuertes.

### Avro: evolución respaldada por un registro de esquemas

Los esquemas de Avro son documentos JSON que describen la forma de un mensaje. Un Schema Registry (el de Confluent es el más común, pero existen alternativas de código abierto) almacena esos esquemas y hace cumplir las reglas de compatibilidad cuando los productores registran nuevas versiones. La biblioteca `fastavro` codifica y decodifica la carga útil binaria. La ruta de publicación es el mismo `broker.publish(...)` que ya conoces; solo cambia el paso de codificación:

**Paso 1 — Declarar el esquema una vez.** `WALLET_DEPOSITED_SCHEMA` enumera cada campo y su tipo Avro (`string`, `long`). Es una constante a nivel de módulo, de modo que se escribe una vez, no por mensaje.

**Paso 2 — Compilarlo una vez.** `fastavro.parse_schema(...)` se llama en tiempo de importación y el resultado se almacena en caché en `_PARSED`. Analizarlo en cada publicación sería trabajo desperdiciado en la ruta caliente.

**Paso 3 — Codificar y publicar.** `fastavro.schemaless_writer` serializa el registro en un búfer `BytesIO`; `buf.getvalue()` entrega los bytes a `broker.publish` exactamente como hacía la ruta JSON.

::: listing lumen/messaging/avro_publisher.py | Listado 10.6 — Publicar un evento de monedero con codificación Avro
from __future__ import annotations

import io

import fastavro  # type: ignore[import]

from pyfly.messaging import MessageBrokerPort

WALLET_DEPOSITED_SCHEMA = {
    "type": "record",
    "name": "FundsDeposited",
    "namespace": "lumen.wallet",
    "fields": [
        {"name": "wallet_id", "type": "string"},
        {"name": "amount", "type": "long"},     # integer minor units
        {"name": "currency", "type": "string"},
        {"name": "balance", "type": "long"},    # new balance, minor units
    ],
}

_PARSED = fastavro.parse_schema(WALLET_DEPOSITED_SCHEMA)


async def publish_deposit_avro(
    broker: MessageBrokerPort,
    wallet_id: str,
    amount: int,
    currency: str,
    balance: int,
) -> None:
    """Encode a FundsDeposited event with Avro and publish to the topic."""
    record = {
        "wallet_id": wallet_id,
        "amount": amount,      # integer minor units
        "currency": currency,
        "balance": balance,
    }
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, _PARSED, record)

    await broker.publish(
        "wallet.events",
        buf.getvalue(),
        headers={"content-type": "avro/binary",
                 "event-type": "FundsDeposited"},
    )
:::

**Cómo funciona.** `fastavro.parse_schema` compila el documento de esquema JSON una vez en el momento de la carga del módulo; nunca lo analices dentro de la función de publicación o pagarás el coste de compilación en cada llamada. `fastavro.schemaless_writer` serializa el registro en el búfer `BytesIO` sin incrustar el esquema en cada mensaje (el registro proporciona el esquema en el lado del consumidor). `buf.getvalue()` extrae los bytes para `broker.publish`.

Las cabeceras `headers={"content-type": "avro/binary", "event-type": "FundsDeposited"}` señalan a los consumidores que se requiere decodificación Avro y transportan el tipo de evento para el enrutamiento, de forma coherente con la convención de JSON.

### Protobuf: rendimiento y poliglotismo

Los Protocol Buffers compilan un archivo `.proto` en una clase generada. Producen mensajes más pequeños que JSON o Avro, y el código generado está disponible en todos los lenguajes principales, lo que convierte a Protobuf en la elección correcta cuando el consumidor es un servicio en Go o Java.

```python
# Assumes a generated class lumen_pb2.FundsDeposited
from lumen_pb2 import FundsDeposited  # type: ignore[import]

event = FundsDeposited(
    wallet_id="w-001",
    amount=5000,      # integer minor units
    currency="EUR",
    balance=10000,
)
payload: bytes = event.SerializeToString()

await broker.publish(
    "wallet.events",
    payload,
    headers={"content-type": "application/protobuf",
             "event-type": "FundsDeposited"},
)
```

Decodificar en el consumidor sigue el patrón espejo:

```python
from lumen_pb2 import FundsDeposited  # type: ignore[import]

event = FundsDeposited()
event.ParseFromString(msg.value)
```

!!! tip "Empieza con JSON, migra cuando sientas el dolor"
    La progresión correcta para la mayoría de los equipos es: JSON primero
    (rápido de entregar, fácil de depurar); añade Avro cuando múltiples
    equipos sean dueños de distintos lados de un topic y la deriva de esquema
    se convierta en un coste de coordinación real; cambia a Protobuf cuando el
    tamaño binario o la interoperabilidad multilenguaje sean un requisito
    estricto. Como el `publish` y el `@message_listener` de PyFly aceptan
    bytes en bruto, puedes cambiar el formato de serialización sin cambiar las
    llamadas a la API del broker: solo intercambia los pasos de codificación y
    decodificación.

---

## Cuando la entrega falla: colas de mensajes muertos

### El inevitable mensaje defectuoso

Incluso un consumidor bien diseñado acabará por encontrarse con un mensaje que no puede procesar. Una base de datos aguas abajo puede no estar disponible. La carga útil puede violar una suposición de la que el consumidor dependía. Un error de red transitorio puede interrumpir una llamada a una API de terceros a mitad del manejador. La pregunta no es si un consumidor fallará, sino qué ocurre cuando lo hace.

Sin una estrategia de mensajes muertos, un consumidor fallido o bien descarta el mensaje (pérdida de datos) o lo vuelve a encolar indefinidamente, creando un bucle infinito de reintentos que bloquea todos los mensajes posteriores: una *píldora envenenada*. Una **cola de mensajes muertos** (DLQ) es la respuesta estructurada: un topic o cola aparte donde los mensajes que no pueden procesarse tras un número configurable de intentos se aparcan para su inspección y reprocesamiento manual.

### Reintento y DLQ nativos del decorador

En PyFly, el reintento y el enrutamiento a mensajes muertos están integrados en `@message_listener`: sin andamiaje de try/except, sin publicación manual en la DLQ. Declara `retries` y `dead_letter_topic` directamente en el decorador. Añades resiliencia añadiendo *argumentos*, no código:

**Paso 1 — Parte de un oyente normal.** El cuerpo del manejador en el siguiente listado es un consumidor corriente que decodifica la carga útil y llama a un trabajador (`_charge`).

**Paso 2 — Añade `retries` (y opcionalmente `retry_delay`).** `retries=3` le indica al framework que reinvoque el manejador hasta tres veces más si lanza una excepción. `retry_delay=0.5` espacia esos intentos con un retroceso lineal.

**Paso 3 — Añade `dead_letter_topic`.** Cuando se agotan todos los reintentos, el framework republica el mensaje allí en lugar de dejar que la excepción tumbe al consumidor. No escribes ni el bucle de reintentos ni la publicación en la DLQ.

::: listing lumen/messaging/resilient_consumer.py | Listado 10.7 — Reintento y DLQ conectados a través de @message_listener
from __future__ import annotations

import json
import logging

from pyfly.container import service
from pyfly.messaging import Message, message_listener

logger = logging.getLogger(__name__)


@service
class ResilientWalletConsumer:
    """Processes wallet events with built-in retry and DLQ fallback."""

    @message_listener(
        topic="wallet.events",
        group="payments-dlq",
        retries=3,
        retry_delay=0.5,              # waits 0.5s, 1.0s, 1.5s (linear)
        dead_letter_topic="wallet.events.DLQ",
    )
    async def on_wallet_event(self, msg: Message) -> None:
        payload = json.loads(msg.value)
        event_type = msg.headers.get("event-type", "unknown")
        logger.info(
            "Processing event: type=%s wallet=%s",
            event_type,
            payload.get("wallet_id"),
        )
        # Any unhandled exception triggers a retry; after 3 retries
        # the original message is forwarded to wallet.events.DLQ.
        await self._charge(payload)

    async def _charge(self, payload: dict) -> None:
        raise NotImplementedError("replace with real payment logic")
:::

**Los tres parámetros:**

`retries=3` reinvoca `on_wallet_event` hasta tres veces más tras el primer fallo. Los reintentos son apropiados para fallos *transitorios* (un único nodo de base de datos reiniciándose); mantén el conteo bajo y deja que la DLQ gestione los fallos sostenidos.

`retry_delay=0.5` aplica un retroceso lineal: el intento 1 espera 0,5 s, el intento 2 espera 1,0 s, el intento 3 espera 1,5 s. Con `retry_delay=0.0` (el valor por defecto), los reintentos son inmediatos.

`dead_letter_topic="wallet.events.DLQ"` es la red de seguridad. Cuando se agotan todos los reintentos, el framework republica el mensaje original en el topic de la DLQ, preservando el `value` y la `key` originales, y añade dos cabeceras de diagnóstico:

| Cabecera | Valor |
|---|---|
| `x-original-topic` | El topic del que se consumió originalmente el mensaje. |
| `x-exception` | El nombre de la clase de la excepción (p. ej., `RuntimeError`). |

La excepción se traga entonces para que el consumidor siga funcionando: el mensaje queda aparcado, no perdido, y el siguiente mensaje del topic se procesa con normalidad.

!!! tip "Ejecútalo"
    Puedes ver cómo un mensaje envenenado aterriza en la DLQ sin un broker
    real. En el momento del cableado, el framework envuelve cada oyente con
    `pyfly.messaging.error_handling.wrap_listener`: el mismo ayudante hace el
    reintento y el envío a mensajes muertos. Acciónalo directamente para que
    el flujo sea visible. Guarda esto como `dlq_demo.py` y ejecuta
    `uv run python dlq_demo.py`:

    ```python
    import asyncio, json
    from pyfly.messaging.adapters.memory import InMemoryMessageBroker
    from pyfly.messaging.error_handling import wrap_listener
    from pyfly.messaging.types import Message

    async def always_fails(msg: Message) -> None:
        raise RuntimeError("downstream unavailable")

    async def main() -> None:
        broker = InMemoryMessageBroker()
        await broker.start()

        async def show_dlq(msg: Message) -> None:
            print("DLQ:", msg.headers["x-original-topic"],
                  msg.headers["x-exception"])
        await broker.subscribe("wallet.events.DLQ", show_dlq)

        handler = wrap_listener(
            always_fails, broker,
            retries=2, dead_letter_topic="wallet.events.DLQ",
        )
        await handler(Message(
            topic="wallet.events",
            value=json.dumps({"wallet_id": "w-001"}).encode(),
        ))
        await broker.stop()

    asyncio.run(main())
    ```

    Tras dos reintentos, el envoltorio se rinde y republica en la DLQ, donde
    tu monitor imprime las cabeceras de diagnóstico:

    ```
    DLQ: wallet.events RuntimeError
    ```

    El manejador retornó con normalidad —la excepción se tragó, no se
    propagó—, así que en una app real el consumidor simplemente pasaría al
    siguiente mensaje.

### Monitorizar la DLQ

Suscríbete al topic de la DLQ como a cualquier otro oyente para observar y alertar sobre los mensajes enviados a mensajes muertos:

::: listing lumen/messaging/dlq_monitor.py | Listado 10.8 — Suscribirse al topic de la DLQ
from __future__ import annotations

import json
import logging

from pyfly.messaging import Message, message_listener

logger = logging.getLogger(__name__)


@message_listener(topic="wallet.events.DLQ", group="dlq-monitor")
async def on_dead_letter(msg: Message) -> None:
    """Log every message that failed all retries."""
    original = msg.headers.get("x-original-topic", "unknown")
    exc = msg.headers.get("x-exception", "unknown")
    payload = json.loads(msg.value) if msg.value else {}
    logger.warning(
        "DLQ message: original_topic=%s exception=%s wallet=%s",
        original,
        exc,
        payload.get("wallet_id"),
    )
:::

!!! warning "Diseña los consumidores para que sean idempotentes"
    Un consumidor que alcanza el límite de reintentos de la DLQ ha consumido
    el mensaje. Si más tarde un operador reproduce el mensaje de la DLQ, el
    consumidor lo procesará de nuevo. Sin idempotencia, ese doble
    procesamiento puede corromper datos: abonar dos veces un monedero, enviar
    una notificación duplicada. Usa el identificador estable del mensaje como
    clave de idempotencia: antes de procesar, comprueba si ese ID ya se ha
    registrado en una tabla `processed_events` y omite el trabajo si así es.
    El paso de comprobar-y-registrar debería estar en la misma transacción de
    base de datos que la escritura de negocio.

---

## Resiliencia: cortacircuitos y reintentos

### Proteger a Lumen de una caída del broker

Un broker sano no está garantizado. Las particiones de red, las actualizaciones progresivas (rolling upgrades) y el agotamiento de recursos pueden hacer que el broker quede temporalmente no disponible. Si el manejador de comandos llama a `broker.publish(...)` y el broker está caído, te enfrentas a dos malas opciones sin una capa de resiliencia: fallar el comando entero (negarte a depositar fondos porque el broker es inalcanzable) o descartar silenciosamente el evento (el depósito tiene éxito pero el evento de integración se pierde).

Ninguna es aceptable. El outbox transaccional (Capítulo 9) es la solución atómica: el evento se captura en la base de datos y un relay lo publica de forma asíncrona, de modo que una caída del broker solo añade latencia, no pérdida de datos. Junto al outbox, los **cortacircuitos** (circuit breakers) y los **reintentos** protegen el relay y cualquier código que llame al broker frente a los fallos en cascada.

Un **cortacircuitos** es la metáfora eléctrica convertida en código: tras demasiados fallos seguidos "salta" y deja de dejar pasar llamadas durante un periodo de enfriamiento, de modo que un broker con dificultades no se vea machacado por miles de intentos de reconexión condenados al fracaso. Un **reintento** es la táctica complementaria: vuelve a probar la misma llamada unas cuantas veces, porque muchos fallos son momentáneos.

El módulo de resiliencia de PyFly (`pyfly.resilience`) proporciona ambas primitivas. El cortacircuitos se abre tras un umbral de fallos configurable y bloquea las llamadas al broker durante un periodo de enfriamiento, evitando una tormenta de reconexiones de manada (thundering herd). El decorador de reintentos gestiona los errores transitorios con un retroceso configurable. Los aplicas como un par de decoradores sobre el método de publicación:

**Paso 1 — Escribe el método de publicación simple.** `forward` hace una sola cosa: codifica el registro y llama a `self._broker.publish(...)`. No vive ninguna lógica de resiliencia en el cuerpo.

**Paso 2 — Envuélvelo en `@circuit_breaker`.** Pasa una instancia compartida de `CircuitBreaker` para que el conteo de fallos se acumule entre llamadas, no por llamada. Cuando el broker está caído, el cortacircuitos salta y falla rápido.

**Paso 3 — Envuelve eso en `@retry` por fuera.** El orden de los decoradores importa: `@retry` se sitúa por encima de `@circuit_breaker`, de modo que todos los intentos de reintento de una llamada ocurren antes de que el cortacircuitos registre un solo fallo. Mantén `max_attempts` bajo y deja que el cortacircuitos absorba las caídas sostenidas.

::: listing lumen/messaging/resilient_publisher.py | Listado 10.9 — Publicación resiliente al broker con reintento y cortacircuitos
from __future__ import annotations

import json
import logging

from pyfly.container import service
from pyfly.messaging import MessageBrokerPort
from pyfly.resilience import CircuitBreaker, circuit_breaker, retry

logger = logging.getLogger(__name__)


@service
class OutboxRelay:
    """
    Drains pending outbox records and forwards them to the broker.
    Applies retry and circuit-breaker protection on every publish call.
    """

    def __init__(self, broker: MessageBrokerPort) -> None:
        self._broker = broker

    @retry(max_attempts=3, delay=1.0, backoff=2.0)
    @circuit_breaker(CircuitBreaker(failure_threshold=5, recovery_timeout=30))
    async def forward(
        self,
        topic: str,
        payload: dict,
        event_type: str,
    ) -> None:
        """Forward a single outbox record to the broker."""
        await self._broker.publish(
            topic,
            json.dumps(payload).encode(),
            headers={"event-type": event_type},
        )
        logger.info(
            "Event forwarded: topic=%s event-type=%s",
            topic,
            event_type,
        )
:::

**Los dos decoradores:**

`@retry(max_attempts=3, delay=1.0, backoff=2.0)` envuelve `forward` en un bucle de reintentos de hasta tres intentos. Tras el primer fallo espera `delay` segundos (1 s); tras el segundo espera `delay * backoff` (2 s): la espera crece como `delay * backoff ** attempt`. Si el tercer intento sigue fallando, la excepción se propaga. Los reintentos sirven para fallos *transitorios* (un único nodo del broker reiniciándose); son contraproducentes para fallos *permanentes* (un topic mal configurado). Mantén `max_attempts` bajo y deja que el cortacircuitos gestione las caídas sostenidas.

`@circuit_breaker(CircuitBreaker(failure_threshold=5, recovery_timeout=30))` protege `forward` con una instancia compartida de `CircuitBreaker` que rastrea los fallos consecutivos a través de todas las llamadas. Cuando el conteo alcanza `failure_threshold`, el circuito se *abre* y las llamadas posteriores fallan inmediatamente con `CircuitBreakerException` en lugar de intentar alcanzar un broker inalcanzable, evitando una tormenta de reconexiones. Tras `recovery_timeout` segundos el circuito entra en un estado *semiabierto*: la siguiente llamada se deja pasar como sonda. Si tiene éxito, el circuito se cierra; si falla, se vuelve a abrir. El orden de los decoradores importa: `@retry` es el decorador externo, de modo que los tres intentos de una llamada lógica ocurren antes de que el cortacircuitos registre un solo fallo.

!!! spring "Equivalencia con Spring"
    `MessageBrokerPort` con `KafkaAdapter` es el equivalente en PyFly del
    `KafkaTemplate` (publicación) y el `@KafkaListener` (consumo) de Spring
    Kafka. `RabbitMQAdapter` refleja el `RabbitTemplate` y el `@RabbitListener`
    de Spring AMQP. El modelo de ciclo de vida es el mismo: registra los
    oyentes antes de arrancar el contenedor, y el framework gestiona los hilos
    de consumidor. Las colas de mensajes muertos en Spring Kafka se configuran
    mediante `DeadLetterPublishingRecoverer` sobre el `DefaultErrorHandler`; en
    Spring AMQP mediante `RabbitListenerContainerFactory` con un
    `MessageRecoverer`. PyFly implementa el mismo patrón de forma declarativa
    a través de `@message_listener(retries=..., dead_letter_topic=...)` en
    lugar de requerir configuración de contenedor específica del broker. Los
    decoradores `@retry` y `@circuit_breaker` reflejan las anotaciones
    `@Retryable` y `@CircuitBreaker` de Resilience4j usadas con la
    infraestructura de mensajería de Spring.

---

## Lo que construiste {.recap}

La Parte III está completa.

Lumen es ahora plenamente orientada a eventos, con event sourcing y conectada a un broker. Aquí está dónde dejó las cosas cada capítulo.

**El Capítulo 8** introdujo el modelo de dos buses: `ApplicationEventBus` para los eventos del ciclo de vida del framework, `InMemoryEventBus` para los eventos de dominio. `EventPublisher` se conectó a los manejadores de comandos para que cada mutación de un agregado produjera un hecho al que oyentes independientes —`WalletAuditListener` entre ellos— pudieran reaccionar sin conocerse entre sí. Las suscripciones usan `@event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])`; los manejadores reciben un `EventEnvelope` cuyo `event_type` es el nombre de la clase del evento de dominio.

**El Capítulo 9** reemplazó el enfoque de agregado mutable más modelo de lectura por el event sourcing. Cada movimiento financiero es un evento inmutable que se añade al libro mayor. El saldo actual se calcula reproduciendo el flujo de eventos. `EventEnvelope` se convirtió en la unidad de almacenamiento, y los snapshots mantuvieron acotados los tiempos de reproducción.

**Este capítulo** cruzó el límite de la red. `MessageBrokerPort` es la única abstracción frente a Kafka, RabbitMQ o el broker en memoria. Intercambiar adaptadores es un cambio de configuración: ningún cambio en el código de negocio. `@message_listener` ofrece suscripciones declarativas y sin código repetitivo tanto en funciones independientes como en métodos de `@service`. Los parámetros `retries` y `dead_letter_topic` gestionan los mensajes envenenados sin andamiaje manual de try/except. Las cargas útiles se codificaron como bytes JSON, con Avro y Protobuf disponibles cuando el cumplimiento de esquema o la eficiencia binaria importan más que la simplicidad. `@retry` y `@circuit_breaker` protegen la ruta de publicación frente a fallos del broker transitorios y sostenidos.

Los eventos de dominio que fluyen a través de los tres capítulos son:

| Clase de evento | Campos |
|---|---|
| `WalletOpened` | `wallet_id`, `owner_id`, `currency` |
| `FundsDeposited` | `wallet_id`, `amount`, `currency`, `balance` |
| `FundsWithdrawn` | `wallet_id`, `amount`, `currency`, `balance` |

`amount` y `balance` son siempre unidades menores enteras (p. ej., `5000` para
50,00 €). `currency` es un valor de cadena del StrEnum `Currency`
(`"EUR"`, `"USD"`, `"GBP"`). El valor de la cabecera `event_type` es siempre el
nombre de la clase —`"FundsDeposited"`—, nunca una ruta con puntos.

Tres principios pasan a la Parte IV:

- **Depende del port, no del adaptador.** `MessageBrokerPort` se inyecta; `KafkaAdapter` es un detalle de configuración.
- **Diseña los consumidores para que sean idempotentes.** Los brokers entregan *al menos una vez*. Protégete frente al procesamiento duplicado con un identificador de mensaje estable.
- **Captura los eventos de forma atómica.** El outbox transaccional garantiza que un evento nunca se pierda, incluso cuando el broker no está disponible en el momento de la escritura.

La Parte IV introduce el `PaymentsService` y el `NotificationsService`. Ambos se suscriben a `wallet.events`. Las elecciones de adaptador y configuración hechas en este capítulo son todo lo que necesitan para empezar a recibir los hechos de Lumen en cuanto se conecten.

---

## Pruébalo tú mismo {.exercises}

!!! note "Ejecútalo"
    Cada ejercicio de abajo termina en una prueba. Como `provider: "memory"`
    no necesita broker, puedes ejecutarlos sin nada instalado más allá del
    extra de desarrollo. Desde la raíz del proyecto Lumen:

    ```bash
    uv run --extra dev pytest tests/test_messaging.py -q
    ```

    Una ejecución en verde tiene este aspecto:

    ```text
    ...                                                                      [100%]
    3 passed in 0.XXs
    ```

    Si una prueba falla con un error de atributo ausente como
    `__pyfly_message_listener__`, el decorador `@message_listener` no está
    aplicado a tu manejador: vuelve a revisar el Paso 2 de "Oyentes
    declarativos".

1. **Intercambia el adaptador en una línea.** Empieza con `provider: "memory"`
   en `pyfly.yaml` y añade el `@message_listener` del Listado 10.4. Escribe
   una prueba de integración que publique un mensaje `FundsDeposited` con
   `amount=5000` y `currency="EUR"` y afirme que el oyente lo recibe. Luego
   cambia a `provider: "kafka"` en el YAML y confirma que la misma prueba (con
   un broker de Kafka gestionado por Testcontainers) pasa sin cambiar el
   oyente ni la aserción de la prueba.

2. **Añade un monitor de DLQ.** Crea un segundo `@message_listener` en el topic
   `wallet.events.DLQ` con el grupo `dlq-monitor`. Debería registrar las
   cabeceras `x-original-topic` y `x-exception` junto con la carga útil
   decodificada. Escribe una prueba que simule un consumidor fallido lanzando
   `RuntimeError` dentro del manejador, configure `retries=2` y
   `dead_letter_topic="wallet.events.DLQ"`, y confirme que el monitor de DLQ
   recibe el mensaje con `x-original-topic: "wallet.events"`.

3. **Evoluciona el esquema con Avro.** Empieza con el
   `WALLET_DEPOSITED_SCHEMA` del Listado 10.6. Añade un campo opcional `note`
   con un valor por defecto de `None` (unión Avro `["null", "string"]`,
   por defecto `null`). Confirma que un consumidor compilado contra el esquema
   original aún puede decodificar un mensaje codificado con el nuevo esquema:
   este es un cambio *retrocompatible* (backward-compatible). Luego intenta
   añadir un campo obligatorio sin un valor por defecto y observa la
   `SchemaParseException` que el registro lanzaría, ilustrando por qué los
   valores por defecto son obligatorios para una evolución segura.
