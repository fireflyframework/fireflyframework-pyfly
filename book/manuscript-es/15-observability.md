<span class="eyebrow">Capítulo 15</span>

# Observabilidad y el panel de administración {.chtitle}

::: figure art/openers/ch15.svg | &nbsp;

En el Capítulo 13 rodeaste las rutas críticas de Lumen con cachés y envolviste las llamadas salientes en cortacircuitos (circuit breakers). En el Capítulo 14 aseguraste cada endpoint con autenticación JWT, guardas de rol y sesiones del lado del servidor.

Lumen ya es rápida y segura, pero sigue siendo una caja negra. Cuando un depósito en un monedero tarda tres segundos en producción necesitas saber *dónde* se fueron esos segundos. Cuando un servicio de pagos aguas abajo se degrada necesitas un panel que se ilumine en rojo *antes* de que se avise a tu ingeniero de guardia. Cuando un auditor de cumplimiento pregunta por qué se rechazó una transferencia concreta necesitas registros de log estructurados con suficiente contexto para reconstruir la decisión.

Este capítulo le añade ojos y oídos a Lumen. Los tres pilares de la observabilidad responden a tres preguntas complementarias sobre un sistema en ejecución:

| Pilar | Pregunta | Módulo de PyFly |
|---|---|---|
| **Logging** | "¿Qué ocurrió y en qué contexto?" | `pyfly.logging` |
| **Métricas** | "¿Cuánto? ¿Con qué rapidez? ¿Con qué frecuencia?" | `pyfly.observability.metrics` |
| **Trazas** | "¿Qué camino siguió esta petición?" | `pyfly.observability.tracing` |

Sobre esos pilares se asienta el **Actuator**: endpoints de gestión para producción que exponen la salud, el cableado de beans, el entorno y los niveles de logger en vivo, y el **panel de administración**, una interfaz de navegador embebida que lo une todo en un único panel de control.

!!! note "Novedad en v26.6.110"
    Dos cambios de esta versión condicionan cómo accedes a todo lo de este
    capítulo. Primero, el Actuator y el panel de administración ahora escuchan en
    un **puerto de gestión separado** —`9090` por defecto— en lugar del puerto de
    aplicación `8080`. Segundo, ese puerto de gestión está **abierto y sin
    autenticar por defecto** (el modelo `management.server.port` de Spring Boot),
    de modo que puedes hacer curl a
    `http://localhost:9090/actuator/health` de inmediato, y lo blindas con
    `pyfly.management.security.enabled: true` cuando despliegas. Iremos señalando
    el puerto correcto en cada paso. Si has leído borradores antiguos que usaban
    `http://localhost:8080/actuator/...`, cambia el puerto a `9090`.

Por último, verás cómo la **programación orientada a aspectos** (AOP) aplica logging y métricas a cada método de servicio de forma declarativa, sin tocar los propios métodos.

Al final del capítulo Lumen producirá logs JSON estructurados con identificadores de correlación y enmascaramiento automático de PII, emitirá métricas Prometheus recolectadas por cualquier colector estándar, propagará spans de trazas OpenTelemetry a través de las fronteras entre servicios, responderá a las sondas de liveness y readiness de Kubernetes y mostrará todo lo anterior en un panel de configuración cero accesible en `/admin`.

---

## Logging estructurado y redacción de PII

### ¿Por qué logging estructurado?

Las líneas de log tradicionales tienen este aspecto:

```
[INFO] Order ord-123 created for customer acme corp (email: sales@acme.com)
```

Buscar `ord-123` en Elasticsearch funciona, hasta que cambia el formato. Y que `sales@acme.com` acabe en un fichero de log puede vulnerar tu política de protección de datos sin que tu equipo siquiera lo note.

El **logging estructurado** sustituye la cadena interpolada por un nombre de evento y pares clave-valor explícitos. Los pares se renderizan como JSON en producción y como `clave=valor` legible en desarrollo. Un sistema de agregación de logs ingiere JSON de forma nativa; consultas sobre `wallet_id` u `owner_id` como campos de primera clase, con independencia del formato del mensaje.

### get_logger

PyFly expone una única función factoría que devuelve un logger estructurado respaldado por `structlog` (cuando el extra `observability` está instalado) o, en caso contrario, un envoltorio de la biblioteca estándar sin dependencias. Ambos aceptan la misma firma de llamada.

!!! note "Jerga: función factoría"
    Una *función factoría* no es más que una función cuya labor es construir y
    devolver un objeto configurado. `get_logger("lumen.wallet")` hace el cableado
    por ti: nunca construyes un logger a mano. La cadena que pasas
    (`"lumen.wallet"`) es el *nombre del logger*; por convención es la ruta de
    módulo con puntos, y es a lo que apuntan las anulaciones de nivel como
    `lumen.wallet: DEBUG`.

Construyamos un pequeño ejemplo que puedas ejecutar y luego pasemos al código real del monedero. Sigue estos pasos.

**Paso 1 — Crea un módulo de demostración desechable.** Dentro de tu proyecto Lumen, crea `src/lumen/logging_demo.py` con el contenido del listado siguiente. (Es un fichero de prueba para aprender; bórralo después: el logging de verdad vive dentro de los manejadores más adelante en el capítulo.)

::: listing lumen/logging_demo.py | Listado 15.1 — Uso del logger estructurado
from pyfly.logging import get_logger

logger = get_logger("lumen.wallet")

logger.info("wallet_opened", wallet_id="wlt-001", owner_id="usr-42")
logger.warning("balance_low", wallet_id="wlt-001", remaining=300)
logger.error(
    "deposit_rejected",
    wallet_id="wlt-001",
    reason="insufficient_funds",
)
:::

**Paso 2 — Ejecútalo.** Ejecuta el módulo directamente:

```bash
uv run python -m lumen.logging_demo
```

En desarrollo, con `format: console`, la salida se lee de forma natural:

```
10:30:00 [info    ] wallet_opened   wallet_id=wlt-001 owner_id=usr-42
10:30:01 [warning ] balance_low     wallet_id=wlt-001 remaining=300
10:30:02 [error   ] deposit_rejected wallet_id=wlt-001 reason=insufficient_funds
```

Fíjate en la forma: el *primer* argumento (`"wallet_opened"`) es el **nombre del evento**, no una frase, y todo lo que viene después es un par `clave=valor`. No hay formateo de cadenas, ni f-string, ni `%s`. Ese es el sentido del logging estructurado: el nombre del evento permanece estable mientras los campos transportan los datos variables.

En producción, con `format: json`, cada línea es un objeto JSON autocontenido:

```json
{"event":"wallet_opened","wallet_id":"wlt-001","owner_id":"usr-42",
 "timestamp":"2026-06-07T10:30:00Z","level":"info",
 "logger":"lumen.wallet"}
```

Configura el logging en `pyfly.yaml`:

```yaml
pyfly:
  logging:
    level:
      root: INFO
      lumen.wallet: DEBUG
      sqlalchemy.engine: WARNING
    format: console          # console | json | logfmt
```

`level.<name>` anula el nivel raíz para cualquier logger cuyo nombre empiece por
ese prefijo. `sqlalchemy.engine: WARNING` silencia los logs de consultas sin tocar
tu código. Una variable de entorno `PYFLY_LOGGING_LEVEL_ROOT=WARNING` anula la
clave de configuración, lo cual resulta útil para builds de staging.

**Qué acaba de pasar.** Llamaste a una sola función, `get_logger`, y obtuviste un
logger que admite campos estructurados `clave=valor`. El ajuste `format` decide
cómo se renderizan esos campos: `console` para humanos en tu terminal, `json` para
máquinas en producción. No escribiste nada de código de handler, formateador ni
appender: PyFly los instaló en el logger raíz al arrancar. Cambia `format: console`
por `format: json` en `pyfly.yaml` y vuelve a ejecutar la demostración para ver los
mismos tres eventos emitidos como un objeto JSON por línea, listos para que un
agregador de logs los ingiera.

!!! tip "¿Por qué no usar directamente el `logging` de la biblioteca estándar?"
    `logging.getLogger("x").info("event", wallet_id="wlt-001")` lanza
    `TypeError`: la biblioteca estándar rechaza los argumentos con nombre.
    `get_logger` garantiza que la firma estructurada funcione sea cual sea el
    adaptador activo.

### Identificadores de correlación

Los **identificadores de correlación** enlazan cada línea de log emitida durante una única petición HTTP. PyFly vincula un `transaction_id` al contexto asíncrono actual de forma automática mediante `TransactionIdMiddleware`. Tus manejadores pueden vincular campos adicionales —como el usuario autenticado— para que esos campos fluyan a través de todas las llamadas de log subsiguientes sin pasarse de forma explícita:

::: listing lumen/wallet/handler.py | Listado 15.2 — Vinculación de contexto de correlación
import structlog

from pyfly.logging import get_logger

logger = get_logger("lumen.wallet")


async def handle_deposit(wallet_id: str, amount: int, owner_id: str) -> dict:
    structlog.contextvars.bind_contextvars(
        wallet_id=wallet_id,
        owner_id=owner_id,
    )

    logger.info("deposit_started", amount=amount)
    # ... business logic ...
    result = {"wallet_id": wallet_id, "new_balance": 1350}
    logger.info("deposit_completed", new_balance=result["new_balance"])

    structlog.contextvars.unbind_contextvars("wallet_id", "owner_id")
    return result
:::

Cada llamada a `logger.*` dentro de `handle_deposit` —incluidas las llamadas en lo más hondo de métodos de servicio aguas abajo— transporta automáticamente `wallet_id` y `owner_id` sin más fontanería.

### Redacción de PII

**PII** son las siglas de *información de identificación personal* (personally identifiable information): correos electrónicos, números de tarjeta, números de documento nacional de identidad y similares. La **redacción de PII** está activada por defecto. Antes de que cualquier registro de log alcance un handler de salida, PyFly escanea el mensaje renderizado en busca de correos, números de tarjeta de crédito, IBAN, SSN, JWT, tokens bearer y credenciales en URL. Los patrones detectados se sustituyen por `<EMAIL>`, `<CREDIT_CARD>`, etc.

Puedes comprobarlo tú mismo en unos segundos. Añade una línea a la demostración del Paso 1:

```python
logger.info("contact_logged", email="alice@example.com")
```

Ejecuta `uv run python -m lumen.logging_demo` de nuevo. La salida muestra el valor ya enmascarado: nunca tuviste que activarlo:

```
10:30:03 [info    ] contact_logged  email=<EMAIL>
```

Esa pasada de redacción se ejecuta para *cada* logger del proceso, incluidos los que están dentro de bibliotecas de terceros, razón por la cual atrapa fugas que tú no escribiste.

El motor de expresiones regulares viene con cada instalación. El motor NER respaldado por Presidio —que además atrapa nombres y direcciones en texto libre— está disponible mediante el extra `[pii]` y se activa automáticamente cuando se instala:

```bash
uv add "pyfly[observability,pii]"
python -m spacy download en_core_web_sm   # lighter model; lg for higher recall
```

Configura la redacción en `pyfly.yaml`:

```yaml
pyfly:
  logging:
    redaction:
      enabled: true          # default; set false to disable entirely
      engine: auto           # regex | presidio | auto (presidio if installed)
      mask: placeholder      # placeholder (<EMAIL>) | partial (****@acme.com)
      deny-fields:
        - password
        - token
        - secret
      presidio:
        score-threshold: 0.6
        languages: [en, es]
```

`deny-fields` enumera los *nombres* de los campos de log estructurado cuyos valores se sustituyen incondicionalmente por `<REDACTED>`. Úsalo para campos como `password`, donde sabes que el valor es sensible sin inspeccionar el contenido.

!!! spring "Equivalencia con Spring"
    Spring Boot no incluye redacción de PII integrada; los equipos integran
    `MaskingMessageConverter` de Logback o appenders personalizados de forma
    manual. La redacción de PyFly se aplica a *todos* los loggers —incluidas las
    bibliotecas de terceros— mediante un único `ProcessorFormatter` /
    `RedactionFilter` instalado en el handler raíz. No hace falta configuración
    por biblioteca.

### Appender de fichero con rotación

Cuando los logs van a un fichero en lugar de a stdout, configura la rotación en `pyfly.yaml`:

```yaml
pyfly:
  logging:
    file:
      name: lumen.log
      path: ./logs
    rolling:
      max-size: 50MB
      max-history: 14
```

PyFly escribe en `./logs/lumen.log` y rota a los 50 MB, conservando 14 ficheros rotados antes de descartar el más antiguo. La misma pasada de redacción de PII se aplica a la salida a fichero.

---

## Métricas

### El MetricsRegistry

Una **métrica** es un número que muestreas a lo largo del tiempo: un recuento de depósitos, una latencia en segundos, una cifra de memoria. **Prometheus** es la base de datos de métricas de código abierto de facto; *recolecta* (lee periódicamente) un endpoint HTTP que tu aplicación expone y almacena los números. **`MetricsRegistry`** es la pequeña puerta de entrada de PyFly a ese mundo: un fino envoltorio sobre la biblioteca `prometheus_client` que garantiza que cada nombre de métrica se registre solo una vez. Las llamadas duplicadas a `counter()` o `histogram()` con el mismo nombre devuelven la métrica existente en lugar de lanzar un `ValueError`. Inyéctalo desde el contenedor de DI (autoconfigurado cuando `prometheus_client` está instalado) o créalo manualmente:

::: listing lumen/observability/metrics.py | Listado 15.3 — Creación de métricas
from pyfly.observability import MetricsRegistry

registry = MetricsRegistry()

deposits_total = registry.counter(
    name="lumen.deposits.total",
    description="Deposit operations completed",
    labels=["status"],
)

deposit_duration = registry.histogram(
    name="lumen.deposits.duration",
    description="Deposit processing time in seconds",
    labels=["status"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
:::

`counter()` y `histogram()` devuelven objetos nativos `prometheus_client.Counter` y `prometheus_client.Histogram`, de modo que cada herramienta del ecosistema Prometheus —paneles de Grafana, reglas de alerta, reglas de grabación— funciona sin modificación.

| Método | Devuelve | Sirve para |
|---|---|---|
| `registry.counter(name, description, labels)` | `Counter` | Recuentos monótonamente crecientes |
| `registry.histogram(name, description, labels, buckets)` | `Histogram` | Duraciones, tamaños, percentiles de latencia |
| `registry.counter(…)` llamado de nuevo | El mismo `Counter` | Deduplicación segura |

### @timed — histograma de duración automático

**`@timed`** registra cuánto tarda en ejecutarse una función asíncrona o síncrona, usando un histograma etiquetado. Funciona sobre cualquier invocable y añade automáticamente las etiquetas `class`, `method` y `exception`.

!!! note "Jerga: counter frente a histogram"
    Un **counter** (contador) solo crece: responde a "¿cuántos?" (depósitos
    atendidos, errores producidos). Un **histogram** (histograma) distribuye en
    cubetas los *valores* observados: responde a "¿cuánto tarda?" o "¿cómo de
    grande?" y permite a Prometheus calcular percentiles (latencia p95). Regla
    general: cuenta eventos con un counter; mide duraciones y tamaños con un
    histogram.

Ahora cableemos la primera métrica real en Lumen. Aquí está el manejador de depósitos que construiste en capítulos anteriores; el único cambio es el decorador sobre `do_handle`.

**Paso 1 — Importa los ayudantes de métricas.** En la parte superior de `src/lumen/core/services/wallets/deposit_funds_handler.py`, añade `MetricsRegistry` y `timed` al import de `pyfly.observability`.

**Paso 2 — Crea un registro a nivel de módulo.** Añade `registry = MetricsRegistry()` por encima de la clase. Como el registro deduplica por nombre, compartir uno por módulo es seguro.

**Paso 3 — Decora `do_handle`.** Coloca `@timed(...)` *por encima* de `@transactional()` para que el temporizador envuelva toda la unidad de trabajo transaccional.

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 15.4 — @timed en DepositFundsHandler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher
from pyfly.observability import MetricsRegistry, timed

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository

registry = MetricsRegistry()


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @timed(registry, "lumen.deposit.duration", "Deposit handler latency")
    @transactional()
    async def do_handle(self, command: DepositFunds) -> int:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

El decorador captura `start = time.perf_counter()`, llama a la función y observa el tiempo transcurrido en el bloque `finally`. La etiqueta `exception` es `"none"` en caso de éxito y el nombre del tipo de excepción en caso de fallo, de modo que puedes desglosar la latencia por resultado en Grafana. Las etiquetas `class` y `method` se derivan automáticamente del nombre cualificado de la función.

Los nombres de los histogramas siguen la convención dot.case de Micrometer: `"lumen.deposit.duration"` se convierte en `lumen_deposit_duration_seconds` en Prometheus, con un sufijo `_seconds` añadido si no está presente.

**Paso 4 — Expón el endpoint de Prometheus.** Por defecto el actuator expone en web
solo `health` e `info` (consulta "El puerto de gestión" más adelante). Añade
`prometheus` a la lista de exposición en `pyfly.yaml` para que aparezca el endpoint
de recolección:

```yaml
pyfly:
  management:
    endpoints:
      web:
        exposure:
          include: "health,info,prometheus"
```

**Paso 5 — Ejecútalo y observa cómo aparece la métrica.** Arranca Lumen, lanza un depósito a través de la API y luego recolecta el puerto de gestión.

```bash
# Terminal 1 — start the app (business API on 8080, management on 9090)
uv run pyfly run --server uvicorn

# Terminal 2 — open a wallet, then deposit into it
WALLET=$(curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'Content-Type: application/json' \
  -d '{"owner_id":"usr-42","currency":"EUR"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['wallet_id'])")
curl -s -X POST localhost:8080/api/v1/wallets/$WALLET/deposit \
  -H 'Content-Type: application/json' -d '{"amount":1350}'

# Now scrape the metric — note the MANAGEMENT port 9090, not 8080
curl -s localhost:9090/actuator/prometheus | grep lumen_deposit_duration
```

Deberías ver líneas de histograma como estas (tus números diferirán):

```
lumen_deposit_duration_seconds_bucket{class="DepositFundsHandler",method="do_handle",exception="none",le="0.05"} 1.0
lumen_deposit_duration_seconds_count{class="DepositFundsHandler",method="do_handle",exception="none"} 1.0
lumen_deposit_duration_seconds_sum{class="DepositFundsHandler",method="do_handle",exception="none"} 0.013
```

La línea `_count` confirma una observación; la línea `_sum` es el total de segundos consumidos. Si `grep` no encuentra nada, es que aún no has lanzado un depósito: la métrica se crea de forma perezosa en la primera llamada.

### @counted — contador de invocaciones automático

**`@counted`** incrementa un contador en cada llamada a la función. El `GetBalanceHandler` de Lumen encaja a la perfección: cada lectura de saldo incrementa el contador, etiquetado por resultado:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listado 15.5 — @counted en GetBalanceHandler
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.observability import MetricsRegistry, counted

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository

registry = MetricsRegistry()


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):

    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    @counted(registry, "lumen.balance.reads", "Balance queries served")
    async def do_handle(self, query: GetBalance) -> BalanceDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

En caso de éxito el contador se incrementa con las etiquetas `class="GetBalanceHandler"`, `method="do_handle"`, `result="success"` y `exception="none"`. En caso de fallo usa `result="failure"` y `exception=<TypeName>`, y luego vuelve a lanzar la excepción original. El nombre del contador recibe automáticamente un sufijo `_total` en Prometheus, según la convención de nombres.

Puedes apilar ambos decoradores en el mismo método. El siguiente listado muestra el `WithdrawFundsHandler` de Lumen cronometrado y contado simultáneamente:

::: listing lumen/core/services/wallets/withdraw_funds_handler.py | Listado 15.6 — Apilando @timed y @counted
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher
from pyfly.observability import MetricsRegistry, counted, timed

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository

registry = MetricsRegistry()


@command_handler
@service
class WithdrawFundsHandler(CommandHandler[WithdrawFunds, int]):
    """Debit funds from a wallet; returns the new balance in minor units."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @timed(registry, "lumen.withdrawal.duration", "Withdrawal latency")
    @counted(registry, "lumen.withdrawals", "Withdrawal attempts")
    @transactional()
    async def do_handle(self, command: WithdrawFunds) -> int:
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet = to_aggregate(entity)
        wallet.withdraw(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

`amount` es un `int` en unidades menores (p. ej. 1050 = 10,50 €); el objeto de valor `Money` impone el tipo. Cada invocación produce tanto una observación de histograma como un incremento de contador.

**Qué acaba de pasar.** Añadiste medición transversal a tres manejadores sin
cambiar nada salvo la pila de decoradores. `@timed` responde a "¿cuánto tardó?",
`@counted` responde a "¿con qué frecuencia y tuvo éxito?", y apilarlos te da ambas
cosas gratis. Los cuerpos de los manejadores —la lógica de negocio en sí— nunca
mencionan métricas. Tras ejecutar unos cuantos depósitos y retiradas puedes
recolectar `localhost:9090/actuator/prometheus` de nuevo y ver cómo
`lumen_withdrawals_total` y `lumen_balance_reads_total` van subiendo.

### Endpoint de recolección de Prometheus

El actuator (que se trata en la siguiente sección) expone el registro de métricas para su recolección. Cuando el actuator está habilitado y `prometheus_client` está instalado, se montan dos endpoints automáticamente sin código adicional:

- `GET /actuator/metrics` — JSON compatible con Micrometer que enumera todos los nombres de métrica
- `GET /actuator/prometheus` — formato de exposición de texto estándar para la recolección

Apunta tus `scrape_configs` de Prometheus a `/actuator/prometheus` y todas las métricas de `MetricsRegistry` aparecerán junto a las métricas de proceso integradas (CPU, memoria, hilos, GC). Por defecto el actuator escucha en el **puerto de gestión** (`9090`), no en el puerto de aplicación (`8080`), así que el destino de recolección es `http://<host>:9090/actuator/prometheus`. Consulta "El puerto de gestión" más adelante.

!!! spring "Equivalencia con Spring"
    `MetricsRegistry` refleja el `MeterRegistry` de Spring procedente de
    Micrometer. `@timed` corresponde al `@Timed` de Spring y `@counted` al
    `@Counted`. Los nombres dot.case (`lumen.deposit.duration`) coinciden con la
    convención de Micrometer que el `/actuator/metrics` de Spring Boot Actuator
    también expone.

---

## Observabilidad de la capa de servidor

!!! note "Novedad en v26.6.113"
    Hasta ahora toda la observabilidad de este capítulo ha sido de la **capa de
    aplicación**: el `MetricsFilter` que mide `http_server_requests_seconds`, los
    filtros de trazas/correlación y `process_metrics`. Esta versión añade métricas
    sobre el **servidor en sí** —el servidor ASGI que ejecuta tu aplicación
    (uvicorn, granian o hypercorn)— para que puedas ver conexiones, peticiones en
    curso, workers y tiempo de actividad junto a las métricas de negocio.

Todas estas métricas se escriben en el mismo registro de Prometheus y se exponen
automáticamente en `/actuator/prometheus`, sin código adicional. Tres mecanismos
cooperan para producirlas:

1. **Un middleware ASGI puro** (`ServerMetricsASGIMiddleware`, en
   `pyfly/web/adapters/starlette/asgi_server_metrics.py`) envuelve la aplicación en
   la **capa más externa** y es la fuente **primaria**: corre en cada worker, para
   cada servidor y cualquier número de workers. Emite `server_active_connections`,
   `server_in_flight_requests` y `server_requests_total`.
2. **Un `ServerMetricsBinder`** (`pyfly/observability/server_metrics.py`), arrancado
   desde el lifespan ASGI dentro del worker (junto a `register_process_metrics` y el
   `ManagementServer`), emite `server_workers` (a partir de la variable de entorno
   `_PYFLY_WORKERS` que fija `pyfly run`), `server_uptime_seconds` (desde que el
   worker se vinculó al socket), `server_started_total`, `server_stopped_total` y,
   opcionalmente, `server_native_connections`.
3. **Un `ServerStatsPort` de mejor esfuerzo** (`pyfly/server/ports/server_stats.py`)
   implementado por cada adaptador: en la ruta en proceso `serve_async`, el
   adaptador de uvicorn aflora su recuento real de conexiones de socket y el total
   de peticiones desde `uvicorn.Server.server_state`; granian y hypercorn solo
   reportan workers y tiempo de actividad (runtime en Rust / sin handle), de modo que
   ahí los campos de conexión son `None`.

!!! note "¿Por qué no leer simplemente las estadísticas nativas del servidor?"
    En la ruta de producción `pyfly run`, `uvicorn.run(workers=N)` bifurca
    subprocesos worker que construyen cada uno su **propio** servidor; el bean del
    adaptador del worker no es el objeto que está sirviendo, así que `server_state`
    queda inalcanzable entre procesos. Por eso el middleware ASGI —que sí corre
    dentro del worker— es la fuente primaria uniforme; las estadísticas nativas son
    un enriquecimiento de mejor esfuerzo.

### Catálogo de métricas

Todos los nombres son nombres de Prometheus y cada medidor lleva las etiquetas
`server` (el tipo de servidor) y `worker_pid`:

| Métrica | Tipo | Qué mide |
|---|---|---|
| `server_active_connections` | gauge | Conexiones ASGI abiertas (http + websocket); aproximado, **no** sockets reales (los sockets keep-alive ociosos que retiene el servidor son invisibles a ASGI) |
| `server_in_flight_requests` | gauge | Peticiones http que se están atendiendo ahora mismo |
| `server_requests_total` | counter | Peticiones http completadas en la capa de servidor |
| `server_workers` | gauge | Procesos worker configurados |
| `server_uptime_seconds` | gauge | Segundos desde que este worker se vinculó al socket (distinto de `process_uptime_seconds`) |
| `server_started_total` / `server_stopped_total` | counter | Ciclo de vida del worker |
| `server_native_connections` | gauge | Recuento **real** de conexiones de socket de uvicorn, incluido keep-alive ocioso (solo en la ruta `serve_async`; ausente en granian/hypercorn) |

### Modo multi-worker

Cuando `workers > 1`, el **modo multiproceso de `prometheus_client`** se activa
automáticamente: `pyfly run` fija `PROMETHEUS_MULTIPROC_DIR` antes de bifurcar los
workers (`pyfly/observability/multiprocess.py`), cada worker escribe sus ficheros
mmap y `/actuator/prometheus` agrega todos los workers mediante
`MultiProcessCollector`. Así, una sola recolección refleja **todos** los workers.

!!! warning "Limitación del modo multiproceso"
    El modo multiproceso solo agrega valores de `Counter`, `Gauge`, `Histogram` y
    `Summary`. Los colectores Python personalizados (las métricas `process_*` y
    `system_*`) **no** se agregan entre workers. Los medidores `server_*` y
    `http_server_requests_*` sí se agregan correctamente.

### Configuración

```yaml
pyfly:
  server:
    observability:
      enabled: true                  # default; lo activan los starters web y core
      sample-interval-seconds: 5.0   # default
      access-log: false              # default; logging de acceso nativo opt-in
```

`pyfly.server.observability.enabled` está activado por defecto por los starters web
y core, igual que `pyfly.observability.metrics.enabled`. Requiere el extra
`observability` (`prometheus_client`); sin él, degrada a no-op.

### Exposición y panel

Los medidores `server_*` aparecen en `/actuator/prometheus` y `/actuator/metrics`.
El panel de administración gana una nueva sección **Observability** en vivo (dentro
de Monitoring): tarjetas de estadística (workers, tiempo de actividad, conexiones
activas, peticiones en curso, peticiones/segundo), gráficos en movimiento, una tabla
de desglose por worker y enlaces a las vistas de Metrics y Traces. Está respaldada
por `GET /admin/api/observability` y el SSE `/admin/api/sse/observability`.

!!! note "Alcance: solo ASGI por ahora"
    Esta versión **no** añade gunicorn: la pila sigue siendo asíncrona, solo ASGI
    (granian > uvicorn > hypercorn). Aun así, el diseño de `ServerStatsPort` y del
    modo multiproceso está preparado para gunicorn de cara a un futuro adaptador.

### Stack local — Prometheus y Grafana

Para que puedas ver estas métricas en vivo, `docker-compose.yml` incorpora servicios
de **prometheus** y **grafana** que recolectan `/actuator/prometheus` (la
configuración vive en `ops/prometheus/prometheus.yml`). Levanta el stack, lanza unas
cuantas peticiones contra la API de negocio y observa cómo `server_in_flight_requests`
y `server_requests_total` se mueven en Grafana.

---

## Trazas distribuidas

### @span — decorador de span de OpenTelemetry

!!! note "Jerga: trace, span, OpenTelemetry"
    Un **span** es un paso de trabajo único, cronometrado y con nombre —"obtener
    el monedero", "persistir el depósito"—. Un **trace** (traza) es el árbol
    completo de spans de una única petición, de la raíz a las hojas.
    **OpenTelemetry** (a menudo "OTel") es el estándar neutral respecto a
    proveedores para producir trazas; una vez que tus spans hablan OTel, cualquier
    visor compatible —Jaeger, Tempo, Honeycomb— puede mostrarlos. PyFly emite
    spans OTel, así que nunca quedas atado a una sola herramienta.

**`@span`** envuelve una función asíncrona o síncrona en un span de OpenTelemetry. Cada span es una unidad de trabajo cronometrada y con nombre. Los spans se anidan automáticamente gracias a la propagación de contexto de OpenTelemetry, de modo que una función decorada con `@span` llamada desde dentro de otra función decorada con `@span` produce una relación padre-hijo en tu visor de trazas:

::: listing lumen/wallet/service.py | Listado 15.7 — @span en métodos de manejador CQRS
from pyfly.observability import span


class DepositFundsHandler:

    @span("deposit-funds")
    async def do_handle(self, command):
        balance = await self._fetch_wallet(command.wallet_id)
        await self._persist_deposit(command.wallet_id, command.amount)
        return balance + command.amount

    @span("fetch-wallet")
    async def _fetch_wallet(self, wallet_id: str) -> int:
        # ... repository.find(wallet_id) ...
        return 1000

    @span("persist-deposit")
    async def _persist_deposit(self, wallet_id: str, amount: int) -> None:
        # ... repository.add(wallet) ...
        pass
:::

En un visor de trazas esto aparece así:

```
deposit-funds  [120 ms]
  +-- fetch-wallet   [15 ms]
  +-- persist-deposit [90 ms]
```

`@span` crea un tracer llamado `"pyfly"` mediante `trace.get_tracer("pyfly")`. Cuando la función decorada lanza, el span registra automáticamente el error: pone el estado en `ERROR`, llama a `current_span.record_exception(exc)` y luego vuelve a lanzar para que los llamantes vean la excepción original sin modificar. Las funciones síncronas se admiten de forma idéntica: no hay `await` en el lado decorado.

### Autoconfiguración de OpenTelemetry

PyFly cablea un `TracerProvider` con un `BatchSpanProcessor` de forma automática
cuando `opentelemetry-api` y `opentelemetry-sdk` están instalados:

```bash
uv add opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp
```

Configura el exportador en `pyfly.yaml`:

```yaml
pyfly:
  observability:
    tracing:
      enabled: true
      service-name: "${pyfly.app.name}"
      exporter: otlp
      otlp:
        endpoint: "http://localhost:4318"
```

**Ejecútalo — observa spans sin un colector.** Levantar Jaeger o Tempo es
excesivo mientras estás aprendiendo. Configura en su lugar el **exportador de
consola**, que imprime cada span por stdout. Lumen viene con
`tracing.enabled: false`; actívalo y elige `console`:

```yaml
pyfly:
  observability:
    tracing:
      enabled: true
      exporter: console
```

Reinicia con `uv run pyfly run --server uvicorn`, lanza un depósito a través de la
API como en el paso de métricas y observa la terminal. Cada `@span` imprime un
bloque JSON que muestra su nombre, el id del span padre y el tiempo transcurrido:
la misma estructura padre-hijo que esboza el diagrama anterior, solo que en forma
de texto. Vuelve a `exporter: otlp` (o elimina la anulación) en cuanto tengas un
colector real.

Reglas de selección del exportador:

- `exporter: otlp` — usa OTLP/HTTP; requiere `opentelemetry-exporter-otlp`
- `exporter: console` — imprime los spans por stdout; útil para depuración local
- `exporter: none` — registra los spans pero los descarta (útil en pruebas)
- Sin definir — selecciona OTLP automáticamente si está configurado
  `pyfly.observability.tracing.otlp.endpoint` o la variable de entorno
  `OTEL_EXPORTER_OTLP_ENDPOINT`; en caso contrario registra una única línea
  informativa y descarta los spans en silencio

!!! tip "TracerProvider personalizado"
    Si necesitas exportación gRPC o una configuración del SDK no estándar,
    registra tu propio `TracerProvider` antes de que PyFly arranque:
    `trace.set_tracer_provider(…)`. PyFly detecta un proveedor global existente y
    omite la autoconfiguración.

### Propagación de contexto — entrante y saliente

Los spans permanecen correlacionados *dentro de* un proceso automáticamente. Mantener la misma traza *a través de* varios servicios requiere extraer el contexto de traza aguas arriba de las cabeceras HTTP entrantes e inyectar el contexto actual en cada llamada saliente.

PyFly gestiona ambos extremos sin nada de código por manejador.

**Entrante — TracingFilter:**

`TracingFilter` se cablea en la cadena de filtros de Lumen inmediatamente después de `CorrelationFilter` por `create_app()`. Para cada petición lee la cabecera W3C `traceparent`, abre un span SERVER como hijo del contexto aguas arriba y mantiene ese span activo durante toda la vida de la petición. Cada `@span` creado durante la petición —y cada línea de log— pertenece a la traza distribuida del llamante:

```python
# Simplified view of what TracingFilter does per-request:
parent = extract_context(request.headers)   # parse W3C traceparent
with tracer.start_as_current_span(
    f"{request.method} {request.url.path}",
    context=parent,
    kind=trace.SpanKind.SERVER,
) as span:
    response = await call_next(request)
    span.set_attribute("http.response.status_code", response.status_code)
```

Cuando OpenTelemetry no está instalado, el filtro es un pasarela transparente.

**Saliente — HttpxClientAdapter:**

`HttpxClientAdapter` llama a `inject_headers()` en cada petición saliente para que los servicios aguas abajo puedan continuar la misma traza:

::: listing lumen/client/inventory_client.py | Listado 15.8 — Propagación de trazas
from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter


class InventoryClient:

    def __init__(self) -> None:
        self._http = HttpxClientAdapter(
            base_url="http://inventory-service:8080"
        )

    async def check_stock(self, sku: str) -> dict:
        # The active traceparent is injected automatically into
        # the outbound request headers — no manual plumbing required.
        resp = await self._http.request("GET", f"/skus/{sku}")
        return resp.json()
:::

**Los logs llevan trace_id y span_id:**

`StructlogAdapter` registra un procesador que estampa los IDs del span activo en cada registro de log. No hace falta ningún cambio de código: cualquier llamada `get_logger(…)` dentro de un span activo obtiene los campos `trace_id` y `span_id` automáticamente:

```json
{
  "event": "deposit_completed",
  "wallet_id": "wlt-001",
  "new_balance": 1350,
  "trace_id": "1a4b3145ed8f2dd11172ee3584123f4a",
  "span_id": "d2a62aaa81b0ad66",
  "timestamp": "2026-06-07T10:30:00Z",
  "level": "info",
  "logger": "lumen.wallet"
}
```

Con `trace_id` en cada registro de log puedes saltar desde una búsqueda en Grafana Loki por `wallet_id=wlt-001` directamente a la vista de traza correlacionada en Tempo, y desde ahí a los gráficos de latencia de Prometheus de esa ventana temporal: los tres pilares unidos por un único identificador.

Los ayudantes de propagación de bajo nivel están disponibles por si alguna vez los necesitas directamente:

```python
from pyfly.observability.propagation import (
    extract_context,   # parse traceparent from inbound headers
    inject_headers,    # write traceparent into outbound headers
    current_trace_ids, # -> (trace_id, span_id) hex, or None
    has_otel,          # True if opentelemetry is importable
)
```

---

## Comprobaciones de salud y el Actuator

::: figure art/figures/15-observability.svg | Figura 15.1 — El Actuator expone la salud, los beans, los loggers y las métricas de Prometheus por HTTP. Las sondas de liveness y readiness de Kubernetes acceden a las subrutas dedicadas.

El **Actuator** le da a Kubernetes y a tu instrumental de operaciones un contrato estable: un conjunto de endpoints de gestión que exponen la salud, el cableado de beans, el estado del entorno y los recolectores de métricas. Lo configuras una vez y cada herramienta, desde `kubectl` hasta Grafana, puede consumirlo sin código a medida.

### Habilitar el Actuator

El Actuator está **activado por defecto** cuando hay presente un contexto de PyFly:
no tienes que habilitarlo en absoluto para obtener `/actuator/health` y
`/actuator/info`. Solo tocas el flag para *desactivarlo* o para ser explícito. Pasa
`actuator_enabled=True` a `create_app()`, o establece el flag en `pyfly.yaml`:

```yaml
pyfly:
  management:
    enabled: true            # default; the actuator is on unless you set false
  app:
    name: lumen
    version: 1.0.0
    description: Lumen wallet service
```

!!! note "La clave de configuración ha cambiado"
    El flag de habilitación ahora es `pyfly.management.enabled`. El antiguo
    `pyfly.web.actuator.enabled` sigue funcionando como alias heredado, pero el
    código nuevo debería usar el espacio de nombres `pyfly.management.*`, que es
    donde también viven los ajustes de puerto, seguridad y exposición de
    endpoints.

Cuando está habilitado, `create_app()` escanea automáticamente el contenedor de DI en busca de beans `HealthIndicator`, crea un `HealthAggregator`, instancia todos los endpoints integrados y los monta en `/actuator/*`.

**Ejecútalo — tu primera comprobación de salud.** Lumen ya habilita el actuator,
así que basta con arrancar la aplicación y hacer curl al endpoint de salud en el
**puerto de gestión**:

```bash
uv run pyfly run --server uvicorn
curl -s localhost:9090/actuator/health
```

Una aplicación sana devuelve HTTP 200 con:

```json
{"status":"UP"}
```

Si obtienes "connection refused", confirma que usaste `9090` (gestión) y no `8080`
(negocio). El mismo `/actuator/health` en `8080` devuelve 404: el puerto de negocio
deliberadamente no transporta endpoints de gestión.

### El puerto de gestión

Por defecto, estos endpoints `/actuator/*` —y el panel `/admin` de la siguiente
sección— se sirven en un **puerto de gestión separado** (`9090`), no en el puerto
de aplicación (`8080`). Este es el modelo `management.server.port` de Spring Boot:
mantener las comprobaciones de salud, la recolección de Prometheus y la consola de
administración fuera del puerto público, exponiendo solo la API de negocio a
internet mientras el instrumental de operaciones accede a `9090` desde dentro del
clúster.

El puerto de gestión es un segundo listener en el mismo proceso (no workers
adicionales), por lo que no añade complejidad de despliegue. Configúralo mediante
`pyfly.management.server.port` (env `PYFLY_MANAGEMENT_SERVER_PORT`): ponlo **igual**
a `pyfly.server.port` para servirlo todo en un solo puerto, o a **`-1`** para
deshabilitar los endpoints web de gestión. Por tanto, un despliegue de Kubernetes
apunta las sondas de liveness/readiness y el `ServiceMonitor` de Prometheus al
puerto `9090`, y el `Service`/`Ingress` para el tráfico de usuario al `8080`.

!!! warning "El puerto de gestión está ABIERTO por defecto"
    A partir de v26.6.110 el puerto de gestión está **sin autenticar por
    defecto**: las rutas `/actuator/*` y `/admin` responden a cualquier llamante
    que pueda alcanzar `9090`. Esto es intencionado (el modelo de Spring Boot): el
    puerto está pensado para ser accesible solo desde dentro de tu clúster, tras
    aislamiento de red, nunca expuesto en la internet pública. Si no puedes
    garantizar ese aislamiento, activa también los filtros de seguridad de la
    aplicación para el puerto de gestión:

    ```yaml
    pyfly:
      management:
        security:
          enabled: true
    ```

    Con ese flag activado, la misma autenticación, guardas de rol y reglas CSRF
    que protegen tu API de negocio también blindan el puerto de gestión.

Por defecto el actuator expone en web solo **`health` e `info`**, de nuevo en
consonancia con Spring Boot, que mantiene los endpoints potencialmente sensibles
(`beans`, `env`, `threaddump`, `prometheus`) fuera de la red hasta que des tu
consentimiento. Amplía el conjunto con
`pyfly.management.endpoints.web.exposure.include`:

```yaml
pyfly:
  management:
    endpoints:
      web:
        exposure:
          include: "health,info,metrics,prometheus,loggers"   # or "*" for all
```

Así pues, los pasos de recolección de Prometheus y de loggers en tiempo de
ejecución, más adelante en este capítulo, dan por hecho que has añadido
`prometheus` y `loggers` a esta lista. El atajo `*` expone todo y es cómodo en
desarrollo.

### Endpoints integrados

| Endpoint | Método | Descripción |
|---|---|---|
| `/actuator` | GET | Índice estilo HAL de todos los endpoints habilitados |
| `/actuator/health` | GET | Estado agregado: `UP` (200) o `DOWN` (503) |
| `/actuator/health/liveness` | GET | Subruta de la sonda de liveness de Kubernetes |
| `/actuator/health/readiness` | GET | Subruta de la sonda de readiness de Kubernetes |
| `/actuator/beans` | GET | Todos los beans de DI registrados con estereotipo y ámbito |
| `/actuator/env` | GET | Perfiles de configuración activos |
| `/actuator/info` | GET | Nombre, versión y descripción de la aplicación |
| `/actuator/loggers` | GET, POST | Lista loggers; cambia niveles en tiempo de ejecución |
| `/actuator/metrics` | GET | Nombres de métrica en JSON compatible con Micrometer |
| `/actuator/prometheus` | GET | Destino de recolección en formato de exposición de texto de Prometheus |
| `/actuator/threaddump` | GET | Instantánea de todos los hilos vivos y sus trazas de pila |
| `/actuator/refresh` | POST | Desaloja los beans de ámbito refresh; revincula la configuración |

### HealthIndicator personalizado

Cualquier bean `@component` con un método `async def health(self) -> HealthStatus` se descubre y registra automáticamente como indicador de salud. El `WalletRepository` de Lumen es un buen candidato: `count()` emite un ligero `SELECT COUNT(*)` contra la sesión de base de datos en vivo sin mutar ningún dato:

::: listing lumen/health/indicators.py | Listado 15.9 — Beans HealthIndicator
from pyfly.actuator import HealthStatus
from pyfly.container import component

from lumen.models.repositories.wallet_repository import WalletRepository


@component
class WalletRepositoryHealthIndicator:
    """Checks the wallet store is reachable with a lightweight probe."""

    def __init__(self, repository: WalletRepository) -> None:
        self._repository = repository

    async def health(self) -> HealthStatus:
        try:
            # count() issues SELECT COUNT(*) — fast, read-only probe.
            total = await self._repository.count()
            return HealthStatus(
                status="UP",
                details={"store": "wallet-repository", "rows": total},
            )
        except Exception as exc:
            return HealthStatus(
                status="DOWN",
                details={"error": str(exc)},
            )


@component
class DatabaseHealthIndicator:
    """Checks database connectivity via a lightweight SELECT 1."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def health(self) -> HealthStatus:
        try:
            async with self._factory() as session:
                await session.execute("SELECT 1")
            return HealthStatus(
                status="UP",
                details={"type": "postgresql", "pool_active": 3},
            )
        except Exception as exc:
            return HealthStatus(
                status="DOWN",
                details={"error": str(exc)},
            )
:::

`HealthStatus.status` admite cuatro valores: `"UP"`, `"DOWN"`, `"OUT_OF_SERVICE"` o `"UNKNOWN"`. El agregador aplica un orden de severidad (`DOWN > OUT_OF_SERVICE > UP > UNKNOWN`) y devuelve el estado del peor caso entre todos los indicadores. Si el método `health()` de algún indicador lanza, ese indicador se trata como `"DOWN"` con `details={"error": "check failed"}`; la excepción se registra pero no hace caer el endpoint de salud.

**Ejecútalo — observa cómo aflora un indicador personalizado.** Coloca el listado
anterior en `src/lumen/health/indicators.py`, reinicia Lumen y pide el informe de
salud detallado (la forma `?show-details`, o simplemente recolectándolo del puerto
de gestión):

```bash
curl -s localhost:9090/actuator/health
```

Una respuesta sana ahora incluye tu componente por su nombre:

```json
{
  "status": "UP",
  "components": {
    "WalletRepositoryHealthIndicator": {
      "status": "UP",
      "details": {"store": "wallet-repository", "rows": 42}
    },
    "DatabaseHealthIndicator": {
      "status": "UP",
      "details": {"type": "postgresql", "pool_active": 3}
    }
  }
}
```

No escribiste código de registro: el estereotipo `@component` más el método
`async def health()` constituyen todo el contrato. Al arrancar, el actuator escaneó
el contenedor, encontró todo lo que parecía un indicador de salud y lo integró en
el agregador.

!!! note "Construir sondas a mano — `app.state.pyfly_health_aggregator`"
    Novedad en v26.6.110: el `HealthAggregator` en vivo es accesible en
    `app.state.pyfly_health_aggregator` en el objeto de aplicación de Starlette.
    Es el *mismo* agregador que usa la ruta `/actuator/health`, tanto si el
    actuator corre en la aplicación principal como en el puerto de gestión
    separado. Si alguna vez necesitas una puerta de readiness a medida —por
    ejemplo, una ruta ASGI personalizada que devuelva 503 hasta que termine un
    calentamiento único—, puedes leer este agregador directamente o registrar
    indicadores adicionales en él después de `create_app()`, en lugar de pasar por
    HTTP. Solo lo expone el adaptador de Starlette.

### Cambiar niveles de log en tiempo de ejecución

El endpoint de loggers te permite inspeccionar y cambiar los niveles de log sin reiniciar Lumen, algo inestimable cuando un incidente de producción necesita salida DEBUG de exactamente un paquete. Primero añade `loggers` a la lista de exposición (`pyfly.management.endpoints.web.exposure.include: "health,info,loggers"`) y luego manéjalo desde el **puerto de gestión** `9090`:

```bash
# List all loggers with configured and effective levels
curl http://localhost:9090/actuator/loggers

# Enable DEBUG for the wallet module — takes effect immediately
curl -X POST http://localhost:9090/actuator/loggers/lumen.wallet \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": "DEBUG"}'

# Reset to inherit from parent
curl -X POST http://localhost:9090/actuator/loggers/lumen.wallet \
  -H "Content-Type: application/json" \
  -d '{"configuredLevel": null}'
```

**Qué acaba de pasar.** Cambiaste el nivel de un logger en un proceso *en marcha*.
El POST surtió efecto de inmediato: sin reinicio, sin redespliegue. En un incidente
real cambiarías exactamente un paquete a DEBUG, capturarías la salida ruidosa que
necesitas y luego harías POST de `null` para devolverlo a como estaba, todo sin
perturbar el resto del servicio.

El endpoint usa el vocabulario de niveles de Spring Boot (`OFF`, `ERROR`, `WARN`, `INFO`, `DEBUG`, `TRACE`) y es compatible sin cambios con el instrumental de Spring Boot Actuator.

### Endpoint de actuator personalizado

Para exponer un endpoint personalizado, implementa el protocolo `ActuatorEndpoint` y anota la clase con `@component`. PyFly lo descubre durante el arranque del contexto y lo monta en `/actuator/{endpoint_id}` automáticamente:

::: listing lumen/actuator/git_info.py | Listado 15.10 — Endpoint de actuator personalizado
from pyfly.container import component


@component
class GitInfoEndpoint:
    """Exposes build metadata at /actuator/git."""

    @property
    def endpoint_id(self) -> str:
        return "git"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context=None) -> dict:
        return {
            "branch": "main",
            "commit": {
                "id": "5c6f83b",
                "time": "2026-06-07T08:30:00Z",
            },
            "build": {
                "version": "1.0.0",
            },
        }
:::

### Configuración de las sondas de Kubernetes

Apunta la especificación de tu pod a las subrutas dedicadas de liveness y readiness para que Kubernetes pueda tomar decisiones independientes de reinicio y de tráfico. Como el actuator vive en el puerto de gestión, las sondas apuntan a **`9090`**, mientras que tu `Service` enruta el tráfico de usuario a `8080`:

```yaml
livenessProbe:
  httpGet:
    path: /actuator/health/liveness
    port: 9090
  initialDelaySeconds: 10
  periodSeconds: 30
readinessProbe:
  httpGet:
    path: /actuator/health/readiness
    port: 9090
  initialDelaySeconds: 5
  periodSeconds: 10
```

Las subrutas separadas te permiten agrupar indicadores de forma independiente: una migración en curso que degrade la readiness no tiene por qué disparar un reinicio de liveness ni la recreación del contenedor.

!!! spring "Equivalencia con Spring"
    El Actuator de PyFly refleja el de Spring Boot. `HealthIndicator`,
    `HealthStatus`, `HealthAggregator`, `ActuatorEndpoint` y `ActuatorRegistry`
    corresponden directamente a sus contrapartes de Spring. El endpoint de loggers
    usa el mismo vocabulario de niveles de Spring Boot y la misma forma de
    respuesta `configuredLevel`/`effectiveLevel`, lo que lo hace compatible con
    Spring Boot Admin y el instrumental compatible con Actuator de fábrica.
    `MetricsAutoConfiguration` y `MetricsActuatorAutoConfiguration` reflejan la
    autoconfiguración de Micrometer de Spring Boot: cuando `prometheus_client`
    está instalado, `/actuator/prometheus` aparece sin ningún cableado manual.

---

## El panel de administración

El **panel de administración** es una interfaz de navegador sin build y sin dependencias servida directamente desde el paquete `pyfly.admin`. Una sola línea de configuración la habilita; navega a `/admin`: sin servidor aparte, sin paso de build de `npm`.

### Habilitar el panel

```yaml
pyfly:
  admin:
    enabled: true
    title: "Lumen Admin"
    theme: auto           # auto | light | dark
    refresh_interval: 5000
```

El panel autodescubre beans, indicadores de salud, loggers, tareas programadas, mapeos HTTP, cachés, manejadores CQRS, sagas y métricas del `ApplicationContext` en ejecución. Los presenta en **15 vistas integradas** con actualizaciones en tiempo real mediante Server-Sent Events (SSE): sin WebSocket, sin bucle de sondeo en tu código.

!!! note "Dónde encontrarlo: el puerto de gestión"
    Como el actuator, el panel se sirve en el **puerto de gestión**. Con los
    valores por defecto de Lumen eso es `http://localhost:9090/admin`, no
    `8080/admin`. Pon `pyfly.management.server.port` igual a `pyfly.server.port`
    si prefieres servir el panel en el mismo puerto que tu API. Y recuerda: ese
    puerto está abierto por defecto; blíndalo con
    `pyfly.management.security.enabled` o con el propio `require_auth` del panel
    (mostrado en "Seguridad") antes de exponerlo en cualquier lugar no fiable.

**Ejecútalo — abre el panel.** Habilítalo en `pyfly.yaml`, arranca Lumen y abre la
URL en un navegador:

```yaml
pyfly:
  admin:
    enabled: true
    title: "Lumen Admin"
```

```bash
uv run pyfly run --server uvicorn
# then visit http://localhost:9090/admin
```

Deberías ver cómo se rellena la vista Overview en un segundo o dos: nombre de la
aplicación y tiempo de actividad, una insignia de salud verde y los recuentos de
beans agrupados por estereotipo. Lanza unos cuantos depósitos a través de
`localhost:8080` y observa cómo los paneles de Salud y Métricas se actualizan en
vivo: la página nunca se recarga, porque los datos llegan por SSE.

!!! note "Jerga: SSE (Server-Sent Events)"
    SSE es un canal de streaming unidireccional: el navegador abre una única
    conexión HTTP de larga duración y el servidor *empuja* eventos por ella a
    medida que ocurren. Es más simple que un WebSocket (que es bidireccional) y es
    justo la herramienta adecuada para un panel que solo necesita *recibir*
    actualizaciones. No escribes ningún bucle de sondeo; el framework gestiona el
    flujo.

### Vistas integradas

**Sección Dashboard:**

| Vista | Descripción |
|---|---|
| Overview | Información de la aplicación, tiempo de actividad, insignia de salud, recuentos de beans por estereotipo |
| Health | Estado de los componentes con insignias UP / DOWN / UNKNOWN codificadas por color; SSE en vivo |

**Sección Application:**

| Vista | Descripción |
|---|---|
| Beans | Todos los beans de DI con estereotipo, ámbito y grafo de dependencias |
| Environment | Perfiles activos y variables de entorno enmascaradas |
| Configuration | Árbol de configuración resuelta para todos los espacios de nombres con seguimiento de origen |
| Loggers | Niveles de logger con interfaz de cambio de nivel en tiempo de ejecución; se admiten TRACE y OFF |

**Sección Monitoring:**

| Vista | Descripción |
|---|---|
| Metrics | CPU, memoria, hilos, GC, tiempo de actividad; métricas de Prometheus opcionales; gráfico de tendencia en vivo |
| Scheduled Tasks | Todas las tareas `@scheduled` con expresiones cron y estado |
| HTTP Traces | Trazas de petición/respuesta con latencia p50/p90/p95/p99 y barra de tasa de error |
| Log Viewer | Cola en vivo con filtros de nivel, búsqueda y pausa/reanudación |

**Sección Infrastructure:**

| Vista | Descripción |
|---|---|
| Mappings | Todas las rutas HTTP con manejador, parámetros, tipo de retorno y docstring |
| Caches | Tipo de adaptador, recuento de entradas, desalojo por clave, desalojo masivo |
| CQRS | Manejadores de comandos y consultas con introspección del pipeline del bus |
| Transactions | DAG de pasos de saga y cobertura de participantes TCC; recuento en curso |

**Sección Fleet (modo servidor):**

| Vista | Descripción |
|---|---|
| Instances | Todas las instancias de aplicación remotas registradas con su estado de salud |

### Flujos SSE en tiempo real

El panel nunca sondea el backend con `setInterval`. Abre una única conexión `EventSource` y el servidor empuja los eventos a medida que ocurren:

| Endpoint SSE | Nombre de evento | Qué transmite |
|---|---|---|
| `/admin/api/sse/health` | `health` | Cambio de estado cada vez que cambia la salud agregada |
| `/admin/api/sse/metrics` | `metrics` | Lista completa de nombres de métrica en cada intervalo de refresco |
| `/admin/api/sse/traces` | `trace` | Trazas HTTP individuales a medida que llegan |
| `/admin/api/sse/logfile` | `log` | Nuevos registros de log del buffer circular en memoria |
| `/admin/api/sse/beans` | `beans` | Instantánea del registro de beans en cada intervalo de refresco |

El buffer circular del visor de logs guarda 2.000 registros; el buffer circular de las trazas HTTP guarda 500. Las rutas de admin y actuator (`/admin/*`, `/actuator/*`) quedan excluidas de la captura de trazas automáticamente, para que no contaminen el panel de trazas.

### Gestión de loggers en tiempo de ejecución

La vista Loggers usa el mismo endpoint `/admin/api/loggers/{name}` que el actuator. Haz clic en una fila de logger para abrir un selector de nivel en línea y envía: el nuevo nivel surte efecto de inmediato, y la interfaz vuelve a obtener los datos para confirmar el cambio. El botón Reset envía `null` para devolver el logger a `NOTSET` (heredar del padre).

### Extensión de vista personalizada

Para añadir tu propia vista de barra lateral, implementa `AdminViewExtension` y anota con `@component`. El panel la descubre al arrancar:

::: listing lumen/admin/deployment_view.py | Listado 15.11 — Vista de administración personalizada
from pyfly.container import component


@component
class DeploymentView:
    """Shows deployment metadata in the admin sidebar."""

    @property
    def view_id(self) -> str:
        return "deployments"

    @property
    def display_name(self) -> str:
        return "Deployments"

    @property
    def icon(self) -> str:
        return "upload-cloud"

    async def get_data(self, context=None) -> dict:
        return {
            "last_deploy": "2026-06-07T08:00:00Z",
            "version": "1.0.0",
            "environment": "production",
        }
:::

`view_id` define el fragmento de URL de la barra lateral (`#deployments`), `display_name` aparece en el menú de la barra lateral e `icon` se mapea a un icono de Feather. `get_data()` es llamado por `GET /admin/api/views` y puede consultar el contenedor de DI, una base de datos o cualquier fuente externa.

### Seguridad

Restringe el acceso al panel a los operadores en producción:

```yaml
pyfly:
  admin:
    enabled: true
    require_auth: true
    allowed_roles:
      - ADMIN
      - OPS
```

Cuando `require_auth: true`, cada ruta `/admin/api/*` —datos, mutación, SSE y endpoints del registro de instancias— requiere un principal autenticado cuyos roles se solapen con `allowed_roles`. Las peticiones no autenticadas reciben `401`; los usuarios autenticados que carezcan de todos los roles listados reciben `403`. El shell SPA estático sigue siendo público para que el panel pueda arrancar y mostrar el mensaje de error.

### Monitorización de flota — modo servidor

Para una flota de instancias de Lumen, ejecuta un servidor de administración dedicado y apunta cada instancia de aplicación hacia él:

```yaml
# Admin server instance
pyfly:
  admin:
    enabled: true
    server:
      enabled: true
      poll_interval: 10000
      instances:
        - name: lumen-1
          url: http://lumen-1:8080
        - name: lumen-2
          url: http://lumen-2:8080
```

```yaml
# Each application instance
pyfly:
  admin:
    enabled: true
    client:
      url: http://admin-server:8080
      auto_register: true
```

`StaticDiscovery` siembra el registro a partir de la lista YAML. `AdminClientRegistration` registra la instancia al arrancar y la elimina al apagar. Las llamadas HTTP usan `httpx` cuando está disponible y recurren a `urllib.request`; los errores de registro se tragan en silencio, de modo que un servidor de administración inalcanzable nunca aborta el arranque de la aplicación.

!!! spring "Equivalencia con Spring"
    PyFly Admin se mapea directamente a Spring Boot Admin. `server.enabled: true`
    sustituye a `@EnableAdminServer`. `client.url` sustituye a
    `spring.boot.admin.client.url`. El frontend de Vaadin/React se sustituye por
    una SPA en JavaScript puro que no requiere ningún instrumental de build. Los
    flujos SSE sustituyen a las notificaciones por WebSocket de Spring Boot Admin.
    El Log Viewer integrado sustituye al visor de logfile de Spring Boot Admin
    respaldado por `/actuator/logfile`; el enfoque de buffer circular de PyFly
    evita la configuración de ruta de fichero que Spring Boot Admin requiere.

---

## AOP para preocupaciones transversales

### ¿Qué es la AOP?

La **programación orientada a aspectos** separa las preocupaciones transversales —logging, métricas, seguridad, auditoría— de la lógica de negocio. Sin AOP, cada método de servicio empieza con `logger.info(...)` y termina con `metrics.increment(...)`. Con AOP, escribes esa lógica una sola vez en una clase `@aspect` y la aplicas a cada método coincidente mediante una expresión de pointcut: los propios métodos quedan limpios.

El módulo de AOP de PyFly trae cinco tipos de advice:

| Advice | Decorador | Se ejecuta |
|---|---|---|
| Before | `@before` | Antes del método destino |
| After returning | `@after_returning` | Después de que el método tenga éxito |
| After throwing | `@after_throwing` | Después de que el método lance |
| After (finally) | `@after` | Siempre, con éxito o fallo |
| Around | `@around` | Envuelve toda la llamada; debe llamar a `jp.proceed()` |

### @aspect — declarar un aspecto

!!! note "Jerga: aspecto, advice, pointcut, weaving"
    Cuatro palabras viajan juntas en AOP. Un **aspecto** es la clase que agrupa
    una preocupación transversal. El **advice** es una pieza concreta de
    comportamiento dentro de él (el cuerpo de `@before`, `@around`, etc.). Un
    **pointcut** es la cadena de patrón —como `"service.*.*"`— que decide *a qué*
    métodos se aplica el advice. El **weaving** (tejido) es el acto de coser el
    advice en esos métodos al arrancar. Tú escribes los aspectos; PyFly hace el
    weaving.

**`@aspect`** marca una clase como aspecto de PyFly. La clase se registra automáticamente en el contenedor de DI como singleton y recibe las dependencias inyectadas vía `__init__`. No se requiere ninguna clase base explícita.

Construye el aspecto de logging en tres movimientos.

**Paso 1 — Declara la clase.** Crea `src/lumen/aspects/logging_aspect.py`, marca la clase con `@aspect` y dale un `logger` a nivel de módulo.

**Paso 2 — Añade métodos de advice.** Cada método se decora con un tipo de advice (`@before`, `@after_returning`, `@after_throwing`) y una cadena de pointcut. El argumento `jp: JoinPoint` transporta los detalles de la llamada interceptada.

**Paso 3 — Establece el orden.** `@order(-50)` hace que este aspecto se ejecute antes que los de número más alto, útil cuando quieres que el logging enmarque el aspecto de métricas que escribes a continuación.

::: listing lumen/aspects/logging_aspect.py | Listado 15.12 — Un aspecto de logging
from pyfly.aop import aspect, before, after_returning, after_throwing, JoinPoint
from pyfly.container.ordering import order
from pyfly.logging import get_logger

logger = get_logger("lumen.audit")


@aspect
@order(-50)
class AuditLoggingAspect:
    """Logs entry, exit, and failure for every service method."""

    @before("service.*.*")
    def log_entry(self, jp: JoinPoint) -> None:
        logger.info(
            "method_called",
            cls=type(jp.target).__name__,
            method=jp.method_name,
        )

    @after_returning("service.*.*")
    def log_return(self, jp: JoinPoint) -> None:
        logger.info(
            "method_returned",
            cls=type(jp.target).__name__,
            method=jp.method_name,
        )

    @after_throwing("service.*.*")
    def log_error(self, jp: JoinPoint) -> None:
        logger.error(
            "method_raised",
            cls=type(jp.target).__name__,
            method=jp.method_name,
            exc=type(jp.exception).__name__,
        )
:::

El pointcut `"service.*.*"` coincide con cada método público de cada bean con estereotipo `@service`. `*` coincide con exactamente un segmento separado por puntos; `**` coincide con uno o más. Se admiten globs parciales dentro de un segmento: `"service.*.do_handle"` coincide con todos los métodos `do_handle` de todos los manejadores con estereotipo de servicio.

Los nombres cualificados siguen el patrón `"{stereotype}.{ClassName}.{method_name}"`, de modo que `service.DepositFundsHandler.do_handle` identifica de forma unívoca el método `do_handle` de `DepositFundsHandler`.

!!! tip "Los handlers `@before` deben ser síncronos"
    Los handlers `@before`, `@after_returning`, `@after_throwing` y `@after`
    siempre son llamados de forma síncrona por el weaver. Solo los handlers
    `@around` pueden ser asíncronos (y deben hacer `await jp.proceed()` cuando
    aconsejan un método asíncrono).

### @around — métricas sin decoradores

**`@around`** es el tipo de advice más potente. Envuelve toda la ejecución del método; llama a `await jp.proceed()` para invocar el método original (o el siguiente advice de la cadena) y añade comportamiento a ambos lados:

::: listing lumen/aspects/metrics_aspect.py | Listado 15.13 — Aspecto de métricas con @around
import time

from pyfly.aop import JoinPoint, around, aspect
from pyfly.container.ordering import order
from pyfly.observability import MetricsRegistry

registry = MetricsRegistry()


@aspect
@order(50)
class MetricsAspect:
    """Records duration and call counts for every service method."""

    @around("service.*.*")
    async def record_metrics(self, jp: JoinPoint):
        start = time.perf_counter()
        exc_name = "none"
        try:
            result = await jp.proceed()
            return result
        except Exception as exc:
            exc_name = type(exc).__name__
            raise
        finally:
            elapsed = time.perf_counter() - start
            histogram = registry.histogram(
                f"service.{jp.method_name}.duration",
                f"Duration of {jp.method_name}",
                labels=["exception"],
            )
            histogram.labels(exception=exc_name).observe(elapsed)
:::

`@order(-50)` en `AuditLoggingAspect` y `@order(50)` en `MetricsAspect` garantizan que el aspecto de logging se dispare primero en la cadena de advice. `HIGHEST_PRECEDENCE = -(2^31)` se ejecuta el primero; `LOWEST_PRECEDENCE = 2^31 - 1` se ejecuta el último.

### Weaving automático — AspectBeanPostProcessor

En producción nunca llamas a `weave_bean()` manualmente. `AopAutoConfiguration` registra **`AspectBeanPostProcessor`** incondicionalmente. Durante el arranque del contexto, el post-procesador:

1. Recopila en un `AspectRegistry` cada bean cuya clase tiene `__pyfly_aspect__ = True`.
2. Para cada bean que no sea aspecto, comprueba si algún pointcut registrado coincide con algún método público.
3. Envuelve los métodos coincidentes en su sitio con la cadena de advice completa mediante `weave_bean()`.

El resultado es AOP de configuración cero: define aspectos, define servicios, arranca la aplicación, y el weaver los cablea entre sí.

**Qué acaba de pasar.** Escribiste dos aspectos —uno para logging, otro para
métricas— y no editaste ni un solo manejador. Al arrancar, `AspectBeanPostProcessor`
hizo coincidir sus pointcuts contra tus beans de servicio y tejió el advice en los
métodos coincidentes en su sitio. A partir de ahora, cada método `@service` emite
logs de entrada/salida y un histograma de duración de forma automática. Añade un
manejador nuevo mañana y heredará la misma observabilidad en el momento en que su
pointcut coincida: nada que recordar, nada que copiar y pegar. Esa es la
recompensa de la AOP: el comportamiento transversal vive en un solo lugar, y el
código de negocio queda limpio.

### Referencia de JoinPoint

Cada handler de advice recibe una dataclass `JoinPoint`:

| Atributo | Disponible en | Descripción |
|---|---|---|
| `target` | Todos | La instancia del bean interceptada |
| `method_name` | Todos | Nombre del método interceptado |
| `args` | Todos | Argumentos posicionales pasados al método |
| `kwargs` | Todos | Argumentos con nombre pasados al método |
| `return_value` | `@after_returning`, `@after` | Valor de retorno (tras el éxito) |
| `exception` | `@after_throwing`, `@after` | La excepción lanzada (o `None`) |
| `proceed` | Solo `@around` | Invocable; con `await` para métodos asíncronos |

### Juntándolo todo — observabilidad completa en DepositFundsHandler

El manejador de depósitos de Lumen con los tres pilares de observabilidad aplicados; cero código de observabilidad dentro de la lógica de negocio:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 15.14 — DepositFundsHandler con observabilidad completa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher
from pyfly.logging import get_logger
from pyfly.observability import MetricsRegistry, counted, span, timed

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository

logger = get_logger("lumen.wallet")
registry = MetricsRegistry()


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """
    Credits funds to an existing wallet and returns the new balance
    (in minor units, e.g. 1350 = €13.50).

    Logging, metrics, and tracing are applied by decorators and aspects;
    the business logic here stays free of cross-cutting concerns.
    """

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @timed(registry, "lumen.wallet.deposit.duration", "Deposit latency")
    @counted(registry, "lumen.wallet.deposits", "Total deposit attempts")
    @span("wallet-deposit")
    @transactional()
    async def do_handle(self, command: DepositFunds) -> int:
        logger.info("deposit_started", wallet_id=command.wallet_id,
                    amount=command.amount)
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)
        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        logger.info("deposit_completed", wallet_id=command.wallet_id,
                    new_balance=wallet.balance.amount)
        return wallet.balance.amount
:::

**Cómo funciona.** `@span` abre un span de OpenTelemetry. `@timed` registra la duración de `do_handle`. `@counted` incrementa el contador de llamadas. `AuditLoggingAspect` dispara `@before` y `@after_returning` en cada método de servicio. `MetricsAspect` añade su propio histograma `@around`. La redacción de PII elimina automáticamente los valores sensibles de la salida de log. El actuator expone `/actuator/health`, `/actuator/prometheus` y `/actuator/loggers`. El panel de administración muestra trazas y registros de log en vivo.

`command.amount` es un `int` en unidades menores, impuesto por el validador del comando `DepositFunds` (`amount > 0`). El objeto de valor `Money` lo envuelve con la `Currency` del monedero, evitando la aritmética entre divisas distintas en la frontera del dominio.

Siete líneas de decoradores y una llamada a `get_logger`, y la ruta de depósito de Lumen es totalmente observable.

**Ejecútalo — confirma que nada se rompió.** Los decoradores de observabilidad
envuelven comportamiento alrededor de tus manejadores; no deben cambiar el
resultado que esos manejadores devuelven. Ejecuta la suite existente de Lumen para
demostrar que la ruta de depósito sigue comportándose igual:

```bash
uv run --extra dev pytest -q
```

Todas las pruebas deberían seguir en verde:

```
.........................................                        [100%]
41 passed in 0.3s
```

Luego da una vuelta manual completa con la aplicación en marcha: lanza un depósito
en `8080`, confirma que `/actuator/health` está `UP` en `9090`, recolecta
`/actuator/prometheus` y comprueba que `lumen_wallet_deposits_total` se incrementa,
y abre `http://localhost:9090/admin` para ver el mismo flujo de eventos pasar por
los paneles en vivo de Métricas y Log Viewer. Los tres pilares —logs, métricas,
trazas— describen ahora un único depósito, unidos por un solo `trace_id`.

---

## Lo que construiste {.recap}

Empezaste con un servicio listo para producción pero opaco. Al final de este
capítulo Lumen:

- Emite **logs JSON estructurados** con identificadores de correlación, campos
  vinculados al contexto asíncrono y redacción automática de PII mediante
  expresiones regulares y, opcionalmente, NER con Presidio.
- Exporta **métricas Prometheus** desde `MetricsRegistry`, etiquetadas con
  histogramas de duración `@timed` y contadores de invocación `@counted`,
  recolectadas en `/actuator/prometheus`.
- Propaga **trazas OpenTelemetry** de extremo a extremo: `TracingFilter` abre un
  span SERVER a partir de la cabecera `traceparent` entrante, `@span` crea spans
  hijos para las llamadas a manejadores, `HttpxClientAdapter` inyecta el contexto
  en las peticiones salientes y `StructlogAdapter` estampa cada registro de log
  con `trace_id` y `span_id`.
- Responde a las **sondas de salud de Kubernetes** en `/actuator/health/liveness`
  y `/actuator/health/readiness` mediante beans `HealthIndicator`
  autodescubiertos, incluido un `WalletRepositoryHealthIndicator` que sondea el
  almacén de monederos.
- Muestra todo lo anterior en el **panel de administración embebido** en `/admin`,
  con 15 vistas integradas, flujos SSE en tiempo real, una cola de logs en vivo,
  un panel de analítica de trazas HTTP y gestión de niveles de logger en tiempo de
  ejecución.
- Aplica logging y métricas de forma **transversal** mediante `@aspect`,
  `@before`, `@after_returning`, `@after_throwing` y `@around`, tejidos
  automáticamente por `AspectBeanPostProcessor` sin tocar el código de los
  manejadores.

## Pruébalo tú mismo {.exercises}

1. **Auditoría de redacción de PII.** Añade una sentencia de log a
   `DepositFundsHandler.do_handle` que incluya una dirección de correo falsa como
   valor de campo (`customer_email="alice@example.com"`) y un campo `token` con una
   cadena arbitraria. Ejecuta Lumen localmente con `format: console`, observa que
   el correo se sustituye por `<EMAIL>` y que el valor del campo token se sustituye
   por `<REDACTED>`. Luego cambia a `engine: presidio` (tras instalar
   `pyfly[pii]` y `en_core_web_sm`) y compara la salida.

2. **HealthIndicator personalizado.** Escribe un `StripeHealthIndicator` que llame
   a `https://status.stripe.com/api/v2/status.json` con `httpx`, parsee
   `indicator.status` y devuelva `UP` si el valor es `"none"` o `DOWN` en caso
   contrario. Regístralo como `@component` y verifica que `/actuator/health`
   incluye un componente `StripeHealthIndicator`. Pruébalo con una llamada `httpx`
   simulada que lance `httpx.ConnectError` y verifica que el estado agregado pasa a
   `DOWN`.

3. **Aspecto de métricas con umbrales por método.** Extiende `MetricsAspect` del
   Listado 15.13 con un `slow_threshold` configurable (0,5 s por defecto)
   inyectado desde `pyfly.yaml` vía `@config_properties`. Cuando un método de
   servicio supere el umbral, emite un `logger.warning("slow_method", …)` con los
   campos `method_name` y `elapsed`. Escribe una prueba de pytest que use un
   `FakeClock` o `unittest.mock.patch("time.perf_counter")` para simular una
   llamada lenta y aserte que se registra la advertencia.
