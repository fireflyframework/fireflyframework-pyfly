<span class="eyebrow">Capítulo 17</span>

# Programación, notificaciones, webhooks y callbacks {.chtitle}

::: figure art/openers/ch17.svg | &nbsp;

En el Capítulo 16 dotaste a Lumen de un arnés de pruebas completo: pruebas unitarias para el dominio, pruebas de flujo CQRS a través del bus real y una prueba del adaptador de SQLite que demuestra una persistencia real. Lumen está ahora bien probado y es resiliente. Pero sigue viviendo enteramente dentro de su propio proceso: reacciona a las peticiones, pero nunca toma la iniciativa por sí mismo.

Las plataformas financieras reales son distintas. Envían extractos de cuenta nocturnos, disparan un SMS en el momento en que los fondos llegan, reciben webhooks de estado de pago de un proveedor de pagos a medianoche y llaman de vuelta a sistemas de socios para confirmar que un desembolso quedó registrado. Eso son cuatro patrones de integración distintos —programación, notificaciones, webhooks entrantes y callbacks salientes— y este capítulo los cubre todos.

Al final del capítulo Lumen:

- ejecutará un **trabajo programado nocturno** que totaliza los saldos diarios de los monederos usando `@scheduled` con una expresión cron;
- enviará un **correo electrónico y una notificación push de "fondos recibidos"** a través de los puertos enchufables `EmailService` y `PushService` de PyFly, disparados por el evento de dominio real `FundsDeposited`;
- aceptará **webhooks entrantes** de un proveedor de pagos ilustrativo, verificando la firma HMAC-SHA256, deduplicando reintentos y despachando a un listener tipado;
- despachará **callbacks salientes** a sistemas de socios, firmando cada carga útil, reintentando ante fallos transitorios y registrando cada intento de entrega.

Instala los dos extras opcionales antes de empezar:

```
uv add "pyfly[scheduling,notifications]"
```

!!! note "Término nuevo: extras opcionales"
    Un *extra* es una porción opt-in de las dependencias de un paquete. `pyfly`
    mantiene su núcleo ligero y entrega capacidades más pesadas —programación,
    notificaciones y el resto— detrás de extras con nombre, de modo que solo
    instalas lo que usas. `pyfly[scheduling,notifications]` trae ambos. La sintaxis
    de corchetes es estándar en el empaquetado de Python; `uv add` lo registra en
    tu `pyproject.toml` para que el siguiente `uv sync` los reinstale. Si más
    adelante ves un `ModuleNotFoundError` para `croniter` o para un proveedor de
    notificaciones, te saltaste esta línea: vuelve a ejecutarla.

Este capítulo está dirigido a PyFly **v26.6.110**. Cada listado de código de abajo
coincide con el código real de Lumen bajo `samples/lumen/src/lumen`, y cada API
del framework se comprobó contra el propio `pyfly`, así que lo que construyas aquí
se ejecuta sin cambios.

---

## Tareas programadas

### ¿Por qué programar en lugar de disparar?

Muchas operaciones de una plataforma financiera no pueden esperar a que llegue una petición HTTP. La conciliación nocturna debe ejecutarse a las 02:00 independientemente de si hay algún usuario activo. Una pasada de calentamiento de caché debería dispararse 30 segundos después del arranque —antes de que llegue el tráfico real— y no cuando la primera petición lenta provoque un fallo de caché. Una métrica de latido debería emitirse cada 10 segundos para que el panel de operaciones muestre una señal en vivo, no una lectura obsoleta.

El módulo de programación de PyFly proporciona una forma declarativa, basada en decoradores, de definir los tres patrones sin gestionar manualmente hilos, bucles de eventos ni ruedas de temporizadores.

::: figure art/figures/17-integrations.svg | Figura 17.1 — Las cuatro\
capas de integración añadidas en este capítulo. Las tareas programadas se disparan\
internamente; las notificaciones fluyen hacia afuera, hacia los usuarios; los webhooks entrantes\
llegan desde los socios; los callbacks salientes cierran el bucle de realimentación.

### El decorador @scheduled

**`@scheduled`** marca cualquier método `async` de un bean `@service` para su ejecución periódica. Acepta exactamente un *disparador* (trigger): `fixed_rate`, `fixed_delay` o `cron`. Proporcionar cero o más de un disparador lanza un `ValueError` en el momento de la decoración, de modo que los errores afloran al arrancar y no silenciosamente a las tres de la madrugada.

!!! note "Término nuevo: disparador"
    Un *disparador* (trigger) es la regla que decide *cuándo* se ejecuta un método
    programado. Eliges exactamente uno: `cron` (una expresión de calendario como
    "todos los días a las 02:00"), `fixed_rate` (un intervalo constante como "cada
    10 segundos") o `fixed_delay` (un hueco medido después de que cada ejecución
    termina). Un método, un disparador.

Construyamos el resumen nocturno una decisión a la vez.

**Paso 1 — Crea el archivo de servicio.** En el árbol de Lumen, añade `daily_rollup.py`
bajo un paquete `ledger`. La clase es un `@service` corriente: una clase Python
sencilla que PyFly registra en su contenedor de inyección de dependencias y
construye por ti. Como es un bean gestionado, puedes pedir el `WalletRepository` en
el constructor y el framework te lo entrega; nunca llamas a `new` tú mismo.

**Paso 2 — Elige el disparador.** Este trabajo debe ejecutarse una vez por noche, a hora fija, así que
el disparador es `cron`. La expresión `"0 2 * * *"` se lee campo a campo como
*minuto 0, hora 2, cada día del mes, cada mes, cada día de la semana* —es decir,
las 02:00 cada día.

**Paso 3 — Escribe el trabajo.** Dentro del método, carga cada monedero, suma los
enteros `balance_minor` persistidos y (por ahora) registra el total. En producción
escribirías la instantánea en una tabla de informes o la enviarías aguas abajo; el
registro mantiene el ejemplo centrado en la *programación*, no en la contabilidad.

::: listing lumen/ledger/daily_rollup.py | Listado 17.1 — Resumen nocturno de saldos de monederos con @scheduled
from datetime import timedelta

from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.scheduling import scheduled


@service
class DailyRollupService:
    """Tallies all wallet balances once per night at 02:00 UTC."""

    def __init__(self, wallet_repo: WalletRepository) -> None:
        self._wallets = wallet_repo

    @scheduled(cron="0 2 * * *")
    async def run(self) -> None:
        wallets = await self._wallets.find_all()
        # find_all() returns WalletEntity rows; balance_minor is the
        # persisted integer (cents).
        total_minor_units = sum(w.balance_minor for w in wallets)
        # Persist or ship the nightly snapshot; here we log it.
        print(
            f"[rollup] {len(wallets)} wallets, "
            f"total {total_minor_units / 100:.2f} "
            f"(minor units: {total_minor_units})"
        )
:::

**Cómo funciona.** `@scheduled(cron="0 2 * * *")` se dispara todos los días a las 02:00 UTC. El planificador calcula `seconds_until_next()` mediante `CronExpression`, duerme exactamente ese tiempo y luego envía el método al ejecutor.

Eso es todo el cableado que Lumen necesita. Con `pyfly[scheduling]` instalado, `SchedulingAutoConfiguration` automáticamente:

1. registra un bean `TaskScheduler`;
2. escanea cada bean `@service` en busca de métodos `@scheduled`;
3. arranca el planificador durante el arranque del `ApplicationContext`;
4. lo detiene con elegancia al apagarse.

No se requiere ningún `SchedulerManager`.

!!! note "Término nuevo: autoconfiguración"
    La *autoconfiguración* es PyFly fijándose en lo que hay en tu classpath y
    cableando por ti la maquinaria correspondiente. `SchedulingAutoConfiguration`
    solo se activa cuando `croniter` (incluido por el extra `scheduling`) es
    importable, así que el planificador aparece en el momento en que instalas el
    extra y permanece ausente en caso contrario. Es la misma idea de "convención
    sobre configuración" que hizo famosa Spring Boot; siempre puedes reemplazar un
    bean declarando el tuyo propio.

**Ejecútalo.** Esperar hasta las 02:00 para ver tu primer tic no tiene gracia, así que
cambia temporalmente el disparador para que se ejecute cada minuto —`@scheduled(cron="* * * * *")`— e
inicia la aplicación desde el directorio `samples/lumen`:

```bash
uv run pyfly run --server uvicorn
```

Al comienzo del siguiente minuto deberías ver tu línea de resumen en los logs (una
base de datos vacía simplemente informa de cero monederos):

```text
[rollup] 0 wallets, total 0.00 (minor units: 0)
```

Abre un monedero y deposita en él (consulta las recetas de curl en el Capítulo 7), espera al
siguiente minuto y los totales se mueven:

```text
[rollup] 1 wallets, total 15.00 (minor units: 1500)
```

Detén la aplicación con Ctrl-C y **vuelve a poner el disparador en `"0 2 * * *"`** antes de
confirmar el cambio: el cron de cada minuto era solo una sonda.

!!! note "Qué acaba de pasar"
    No arrancaste un hilo, ni abriste un bucle de eventos, ni registraste un
    temporizador. Escribiste un `@service` con un método `@scheduled`, y el
    framework lo descubrió al arrancar, calculó el siguiente momento de disparo a
    partir de la expresión cron, durmió hasta entonces y ejecutó tu corrutina,
    repitiendo para siempre. El planificador es *declarativo*: tú indicas *cuándo*,
    PyFly se encarga del *cómo*.

### fixed_rate frente a fixed_delay

**`fixed_rate`** mide desde el **inicio** de una ejecución hasta el inicio de la siguiente. **`fixed_delay`** mide desde el **final** de una ejecución hasta el inicio de la siguiente. Usa `fixed_rate` para latidos y métricas donde necesitas una cadencia constante con independencia del tiempo de ejecución. Usa `fixed_delay` cuando necesitas un hueco de respiro garantizado; por ejemplo, al sondear una API aguas arriba que limita la tasa por frecuencia de peticiones.

::: listing lumen/health/monitor.py | Listado 17.2 — Latido fixed_rate y sondeo fixed_delay
from datetime import timedelta

from pyfly.container import service
from pyfly.scheduling import scheduled


@service
class HealthMonitor:

    @scheduled(
        fixed_rate=timedelta(seconds=10),
        initial_delay=timedelta(seconds=5),
    )
    async def heartbeat(self) -> None:
        """Emit a liveness metric every 10 s, starting 5 s after startup."""
        # metrics.gauge("lumen.up", 1)
        pass


@service
class ExchangeRatePoller:

    def __init__(self, fx_repo) -> None:
        self._repo = fx_repo

    @scheduled(fixed_delay=timedelta(minutes=5))
    async def poll(self) -> None:
        """Fetch the latest exchange rates, then wait 5 min before repeating."""
        rates = await self._repo.fetch_latest()
        await self._repo.store(rates)
:::

`initial_delay` pospone la primera ejecución; está disponible tanto para `fixed_rate` como para `fixed_delay` (se ignora en los disparadores `cron`, que siempre esperan al siguiente instante de calendario coincidente).

### CronExpression

**`CronExpression`** también es utilizable directamente: resulta conveniente cuando necesitas mostrar próximos horarios de programación en una interfaz o validar una expresión proporcionada por el usuario antes de almacenarla.

::: listing lumen/ledger/schedule_preview.py | Listado 17.3 — Uso de CronExpression de forma independiente
from pyfly.scheduling import CronExpression


def preview_rollup_schedule(expression: str, n: int = 5) -> list[str]:
    """Return the next N fire times for a given cron expression."""
    cron = CronExpression(expression)
    return [str(t) for t in cron.next_n_fire_times(n)]
:::

**Ejecútalo.** `CronExpression` no necesita una aplicación en marcha, así que la forma más rápida de construir
intuición es el REPL. Desde `samples/lumen`:

```bash
uv run python -c "from pyfly.scheduling import CronExpression; \
print(*CronExpression('0 2 * * *').next_n_fire_times(3), sep='\n')"
```

Deberías ver los tres próximos instantes de las 02:00, uno por línea (tus fechas
serán distintas):

```text
2026-06-16 02:00:00+00:00
2026-06-17 02:00:00+00:00
2026-06-18 02:00:00+00:00
```

Fíjate en el `+00:00`: los momentos de disparo son UTC salvo que pases una `zone` (que se cubre
a continuación). Esta es también la forma más limpia de comprobar la cordura de una expresión proporcionada
por el usuario antes de almacenarla: una cadena no válida lanza `ValueError` de inmediato.

`CronExpression` acepta tanto el formato estándar de 5 campos (`min hour dom month dow`) como el formato de 6 campos al estilo Spring con los segundos primero (`sec min hour dom month dow`). El comodín `?` de Spring se normaliza a `*` de forma transparente.

| Expresión | Se dispara |
|---|---|
| `* * * * *` | Cada minuto |
| `0 * * * *` | Cada hora, en punto |
| `0 2 * * *` | Cada día a las 02:00 |
| `0 9 * * 1-5` | Días laborables a las 09:00 |
| `30 2 1 * *` | El día 1 de cada mes a las 02:30 |
| `*/15 * * * *` | Cada 15 minutos |
| `0 0 12 * * *` | Mediodía cada día (6 campos, segundos primero) |

### Cron consciente de la zona horaria

Las expresiones cron se evalúan en **UTC** por defecto. Pasa `zone` con un nombre de zona horaria IANA para evaluar los momentos de disparo en una zona específica en su lugar:

```python
@scheduled(cron="0 2 * * *", zone="America/New_York")
async def close_books(self) -> None:
    """02:00 New York time — follows DST automatically."""
    ...
```

El mismo parámetro `zone` está disponible en `CronExpression`:

```python
cron = CronExpression("0 9 * * *", zone="Europe/Madrid")
next_run = cron.next_fire_time()  # zone-aware datetime
```

Las transiciones de horario de verano (DST) las gestiona la base de datos `zoneinfo`; no se requiere ningún ajuste manual de desfase.

### Bloqueo distribuido

Cuando varias instancias de Lumen se ejecutan detrás de un balanceador de carga, cada instancia programa los mismos métodos `@scheduled`. Sin coordinación, el resumen nocturno se dispara una vez por instancia y escribe registros duplicados. El parámetro `lock` resuelve esto de la misma manera que `@SchedulerLock` (ShedLock) de Spring: antes de cada tic el planificador intenta adquirir un bloqueo con nombre y **omite la ejecución** si el bloqueo ya está retenido en otro lugar.

```python
@scheduled(cron="0 2 * * *", lock=True, lock_ttl=timedelta(minutes=5))
async def run(self) -> None:
    """lock=True auto-names the lock 'DailyRollupService.run'."""
    ...
```

- `lock=True` — deriva el nombre del bloqueo de `"ClassName.method_name"`.
- `lock="shared-name"` — nombre explícito; útil cuando dos métodos deben ser mutuamente excluyentes.
- `lock_ttl` — TTL de válvula de seguridad; ponlo cómodamente más largo que el peor tiempo de ejecución del trabajo.

Por defecto `TaskScheduler` usa `LocalLock`, que siempre adquiere: el comportamiento de instancia única no cambia. Para la coordinación entre procesos, implementa `DistributedLock` y regístralo como un bean:

::: listing lumen/infra/redis_lock.py | Listado 17.4 — DistributedLock respaldado por Redis
from pyfly.container import bean, configuration
from pyfly.scheduling import DistributedLock


class RedisLock:
    """Best-effort named lock backed by Redis SET NX PX."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def try_acquire(self, name: str, ttl: float) -> bool:
        ok = await self._redis.set(
            f"pyfly:lock:{name}", "1",
            nx=True, px=int(ttl * 1000),
        )
        return ok is True

    async def release(self, name: str) -> None:
        await self._redis.delete(f"pyfly:lock:{name}")


@configuration
class LockConfig:

    @bean
    def distributed_lock(self) -> DistributedLock:
        import redis.asyncio as aioredis
        client = aioredis.from_url("redis://localhost:6379/1")
        return RedisLock(client)
:::

`SchedulingAutoConfiguration` detecta el bean `DistributedLock` en el contenedor automáticamente y se lo pasa al `TaskScheduler`. Cualquier objeto con corrutinas `try_acquire` y `release` conformes satisface el protocolo.

### @async_method

**`@async_method`** marca un método para ejecución fire-and-forget (dispara y olvida) a través del `TaskExecutorPort`. La persona que llama retorna de inmediato; el framework encamina la corrutina a través del ejecutor configurado en segundo plano:

```python
from pyfly.scheduling import async_method


@service
class AlertService:

    @async_method
    async def send_alert(self, msg: str) -> None:
        """Caller does not await — AlertService dispatches asynchronously."""
        ...
```

Bajo el capó `@async_method` establece `__pyfly_async__ = True` en la función; el framework detecta esta bandera y envía la corrutina al `TaskExecutorPort`.

!!! spring "Equivalencia con Spring"
    `@scheduled(fixed_rate=...)` refleja el
    `@Scheduled(fixedRate=...)` de Spring. `@scheduled(fixed_delay=...)` refleja
    `@Scheduled(fixedDelay=...)`. `@scheduled(cron=...)` refleja
    `@Scheduled(cron=...)`. `zone=` refleja el atributo `zone` de Spring.
    `lock=True` refleja el `@SchedulerLock` de ShedLock. `@async_method`
    refleja el `@Async` de Spring.

### Referencia de configuración

```yaml
pyfly:
  scheduling:
    enabled: true          # set false to disable all loops
    executor:
      type: asyncio        # 'asyncio' (default, in-loop) or 'thread'
      max-workers: 4       # worker threads when type is 'thread'
    lock:
      provider: none       # none | memory | redis | postgres
```

Cuando `enabled` es `false`, `TaskScheduler` no arranca ningún bucle y todos los métodos `@scheduled` se omiten en silencio.

El `executor.type` elige cómo se ejecuta cada tic. El valor por defecto `asyncio` ejecuta la
corrutina en el bucle de eventos de la aplicación —ideal para trabajos cortos, ligados a E/S, como
el resumen—. Cambia a `thread` (un grupo de `executor.max-workers` hilos) cuando un
trabajo realice trabajo intensivo de CPU o llame a una biblioteca bloqueante, de modo que no pueda atascar el bucle.

!!! tip "Cómo elegir un proveedor de bloqueo"
    `lock.provider` selecciona el backend detrás de `@scheduled(lock=...)`, descrito
    a continuación: `none` (el valor por defecto: sin coordinación), `memory` (exclusión mutua
    dentro de un proceso), `redis` o `postgres` (verdadera coordinación entre instancias
    sin cambios en el código). En `redis`/`postgres` PyFly construye el
    bean `DistributedLock` por ti a partir de `pyfly.scheduling.lock.redis.url` o el
    `AsyncEngine` ya existente de la aplicación; el `@bean` artesanal del Listado 17.4 es la
    alternativa de hazlo-tú-mismo cuando necesitas semánticas personalizadas.

---

## Notificaciones

Lumen necesita decirles a los clientes que su dinero ha llegado: un correo electrónico para la confirmación del saldo y, opcionalmente, un SMS o un push móvil para la alerta en tiempo real.

El módulo de notificaciones de PyFly define tres **protocolos de puerto** y tres **servicios por defecto**. Tu lógica de negocio depende de los protocolos; los adaptadores concretos de proveedor —SMTP, SendGrid, Twilio, Firebase— viven detrás de la frontera del puerto y pueden intercambiarse sin tocar una sola línea de código de dominio.

!!! note "Término nuevo: puerto y adaptador"
    Un *puerto* es una interfaz con la que tu código conversa —aquí, "algo capaz de
    enviar un correo electrónico"—. Un *adaptador* es una implementación concreta de ese puerto:
    `SmtpEmailProvider`, `SendGridEmailProvider`, etcétera. El patrón (también
    llamado *arquitectura hexagonal*) significa que tu lógica de depósito depende solo del
    puerto `EmailService`, nunca de un proveedor específico. Cambiar SMTP por SendGrid
    es un cambio de una línea en una clase de configuración; el código de dominio nunca se entera.

### La jerarquía de puertos

| Protocolo | Clase de servicio | Método |
|---|---|---|
| `EmailProvider` | `DefaultEmailService` | `send(EmailMessage) -> NotificationResult` |
| `SmsProvider` | `DefaultSmsService` | `send(SmsMessage) -> NotificationResult` |
| `PushProvider` | `DefaultPushService` | `send(PushMessage) -> NotificationResult` |

`DefaultEmailService`, `DefaultSmsService` y `DefaultPushService` son envoltorios finos: cada uno delega en un proveedor, captura cualquier excepción del proveedor y devuelve un `NotificationResult` estructurado con `status=FAILED` y la cadena del error en lugar de propagar la excepción. Una caída transitoria de SendGrid no tumba el manejador (handler) de depósitos.

### Mensajes y resultados

`FundsDeposited` lleva `amount` y `balance` como **unidades menores enteras** (céntimos). La propiedad `Money.major_units` las convierte para su visualización —`Money(25000, Currency.EUR).major_units` es `250.0`—. Tenlo en cuenta al formatear los cuerpos de las notificaciones.

::: listing lumen/notifications/models_overview.py | Listado 17.5 — Los DTO principales
from pyfly.notifications import (
    EmailMessage,
    NotificationResult,
    PushMessage,
    SmsMessage,
)

# Email — full field set
email = EmailMessage(
    to=["alice@example.com"],
    sender="no-reply@lumenbank.com",
    subject="Funds received",
    body_text="EUR 250.00 has been credited to your wallet.",
    body_html=(
        "<p><strong>EUR 250.00</strong> has been credited "
        "to your wallet.</p>"
    ),
)

# SMS — compact
sms = SmsMessage(
    to="+34600000001",
    body="Lumen: EUR 250.00 received. New balance: EUR 750.00.",
    sender="LUMEN",
)

# Push — structured payload
push = PushMessage(
    device_tokens=["FCM_TOKEN_GOES_HERE"],
    title="Funds received",
    body="EUR 250.00 credited",
    data={"wallet_id": "w-001", "amount_minor": 25000},
)
:::

`NotificationResult` lleva `id`, `provider`, `status` (`EmailStatus.SENT | DELIVERED | FAILED | ...`), un `provider_id` opcional (p. ej. el ID de mensaje de SendGrid) y un `error` opcional.

### Cableando el proveedor SMTP

Para desarrollo y despliegues autoalojados, `SmtpEmailProvider` ejecuta `smtplib` desde un grupo de hilos para que el bucle de eventos asíncrono nunca se bloquee.

**Paso 1 — Construye el proveedor como un `@bean`.** Una clase `@configuration` es el lugar
de PyFly para ensamblar objetos que el contenedor no puede construir por sí solo —aquí, un
cliente SMTP que necesita un host, credenciales y ajustes de TLS—. Cada método `@bean`
devuelve un objeto listo para usar; el framework lo cachea y lo inyecta
allá donde se solicite el tipo de retorno.

**Paso 2 — Envuélvelo en un `DefaultEmailService`.** El segundo `@bean` toma el
proveedor y lo devuelve como un `EmailService` —el *puerto* del que depende tu código
de dominio—. El tipo de retorno declarado importa: al devolver `EmailService`, cada
clase que pide un `EmailService` recibe este envoltorio, y ninguna de ellas
se entera de que SMTP está detrás.

::: listing lumen/notifications/config.py | Listado 17.6 — Proveedor SMTP cableado como un @bean
from pyfly.container import bean, configuration
from pyfly.notifications import DefaultEmailService, EmailService
from pyfly.notifications.providers.smtp import SmtpEmailProvider


@configuration
class NotificationConfig:

    @bean
    def email_provider(self) -> SmtpEmailProvider:
        return SmtpEmailProvider(
            "smtp.lumenbank.internal",
            port=587,
            username="notifications",
            password="s3cr3t",
            use_tls=True,
        )

    @bean
    def email_service(
        self, provider: SmtpEmailProvider,
    ) -> EmailService:
        return DefaultEmailService(provider=provider)
:::

`SmtpEmailProvider` acepta `host`, `port` (por defecto `587`), `username`, `password` y `use_tls` (por defecto `True`). Cámbialo por `SendGridEmailProvider` o `ResendEmailProvider` modificando un único método `@bean`: `DefaultEmailService` es indiferente al proveedor que tenga detrás.

!!! tip "Proveedores disponibles"
    `pyfly.notifications` incluye ocho adaptadores integrados:
    `DummyEmailProvider` / `DummySmsProvider` / `DummyPushProvider`
    (solo registran, para desarrollo/pruebas), `SmtpEmailProvider`,
    `SendGridEmailProvider`, `ResendEmailProvider` (correo electrónico),
    `TwilioSmsProvider` (SMS) y `FirebasePushProvider` (push). Todos
    satisfacen su respectivo protocolo `*Provider`.

### Enviando una notificación de "fondos recibidos"

Lumen publica un evento de dominio `FundsDeposited` cada vez que el comando `deposit()` tiene éxito (consulta el Capítulo 8). El lugar adecuado para disparar la notificación es un listener de EDA suscrito a ese evento, no el propio manejador del comando, lo que mantiene la ruta de depósito libre de preocupaciones de notificación.

`FundsDeposited` lleva `wallet_id: str`, `amount: int` (unidades menores), `currency: str` y `balance: int` (nuevo saldo, unidades menores). El listener convierte `amount` en una cadena de visualización mediante `amount / 100`.

**Paso 1 — Suscríbete al evento.** Apila `@event_listener(event_types=["FundsDeposited"])`
sobre un método `async` de un `@service`. Al arrancar, PyFly descubre el método
marcado y lo autosuscribe al bus `EventPublisher` —exactamente el mismo
mecanismo que `WalletAuditListener` usó allá en el Capítulo 8—. Nunca cableas un bus a
mano.

**Paso 2 — Lee la carga útil.** El manejador recibe un `EventEnvelope`. Su
`payload` es un dict sencillo de los campos del evento, así que extraes `wallet_id`,
`amount`, `currency` y `balance` con `.get(...)` y coaccionas los importes
a enteros. Como los importes están en unidades menores, dividir por 100 da el valor
de visualización: `25000` se convierte en `250.00`.

**Paso 3 — Envía a través de los puertos.** Inyecta `EmailService` y `PushService` en
el constructor y llama a `.send(...)` en cada uno. Ambos devuelven un `NotificationResult`
en lugar de lanzar una excepción: un proveedor inestable se degrada con elegancia en lugar de hacer caer
el listener.

::: listing lumen/wallet/deposit_notification_listener.py | Listado 17.7 — Notificando ante FundsDeposited
from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener
from pyfly.notifications import (
    EmailMessage,
    EmailService,
    PushMessage,
    PushService,
)


@service
class DepositNotificationListener:
    """Sends email + push when a FundsDeposited event is observed."""

    def __init__(
        self,
        email_service: EmailService,
        push_service: PushService,
    ) -> None:
        self._email = email_service
        self._push = push_service

    @event_listener(event_types=["FundsDeposited"])
    async def on_funds_deposited(
        self, envelope: EventEnvelope,
    ) -> None:
        payload = envelope.payload
        wallet_id = str(payload.get("wallet_id", ""))
        amount_minor = int(payload.get("amount", 0))
        currency = str(payload.get("currency", "EUR"))
        balance_minor = int(payload.get("balance", 0))
        amount_str = f"{amount_minor / 100:.2f} {currency}"
        balance_str = f"{balance_minor / 100:.2f} {currency}"

        # Fetch contact details from a wallet profile service in prod;
        # hardcoded here for illustration.
        email = "customer@example.com"
        device_token = "FCM_TOKEN_GOES_HERE"

        await self._email.send(EmailMessage(
            to=[email],
            sender="no-reply@lumenbank.com",
            subject=f"Funds received: {amount_str}",
            body_text=(
                f"{amount_str} has been credited to wallet "
                f"{wallet_id}. New balance: {balance_str}."
            ),
        ))
        await self._push.send(PushMessage(
            device_tokens=[device_token],
            title="Funds received",
            body=f"{amount_str} credited",
            data={
                "wallet_id": wallet_id,
                "amount_minor": amount_minor,
                "currency": currency,
            },
        ))
:::

Ambas llamadas devuelven un `NotificationResult`; inspecciona el campo `status` para registrar fallos o programar reintentos.

**Ejecútalo.** No quieres un servidor SMTP real mientras desarrollas, así que cambia el
proveedor por el `DummyEmailProvider`, que solo registra. En tu clase `@configuration`,
devuelve un `DummyEmailProvider` (y un `DummyPushProvider`) en lugar del de SMTP,
luego inicia la aplicación y dispara un depósito:

```bash
uv run pyfly run --server uvicorn
# in a second terminal, open a wallet and deposit (see Chapter 7), e.g.:
curl -s -X POST localhost:8080/api/v1/wallets/<wallet-id>/deposit \
  -H 'content-type: application/json' -d '{"amount":25000}'
```

El depósito publica `FundsDeposited`, el listener se dispara y los proveedores
de prueba registran los mensajes que "enviaron":

```text
[dummy email] to=['customer@example.com'] subject=Funds received: 250.00 EUR
[dummy push] tokens=1 title=Funds received
```

El `DummyEmailProvider` también conserva cada mensaje que recibió en una lista `.sent`,
que es exactamente contra lo que se afirma la prueba del Ejercicio 2, sin necesidad de servidor SMTP.

!!! note "Qué acaba de pasar"
    El comando de depósito no sabía nada del correo electrónico. Simplemente hizo su trabajo y
    lanzó un evento de dominio `FundsDeposited`. La lógica de notificación vive en un
    listener aparte que *reacciona* a ese evento, así que la ruta de depósito se mantiene
    limpia y puedes añadir, eliminar o cambiar notificaciones sin tocar el
    manejador del comando. Esa separación —publicar un hecho, dejar que las partes interesadas
    reaccionen— es la razón de ser de la arquitectura orientada a eventos.

!!! spring "Equivalencia con Spring"
    `EmailService` / `SmsService` / `PushService` son los equivalentes en Python
    del `JavaMailSender` (correo electrónico) de Spring y de las integraciones
    de terceros de Spring para SMS y push. La división hexagonal puerto/adaptador
    es idéntica: tu código de dominio depende del protocolo;
    los adaptadores concretos de proveedor viven en la capa de infraestructura.

---

## Webhooks entrantes

Un proveedor de pagos ilustrativo envía por POST un evento `payment_intent.succeeded` a Lumen cada vez que un cliente recarga su monedero con tarjeta. Lumen debe:

1. **verificar la firma HMAC-SHA256** para rechazar cargas útiles falsificadas;
2. **deduplicar** los reintentos usando la clave de idempotencia para que un reintento no acredite un monedero dos veces;
3. **despachar** el evento verificado a un listener tipado.

El módulo `pyfly.webhooks` de PyFly se encarga de los tres pasos.

!!! note "Términos nuevos: webhook, HMAC, idempotencia"
    Un *webhook* es un POST HTTP que un sistema externo te envía *a ti* cuando
    ocurre algo: el espejo entrante de los callbacks salientes que veremos más adelante en
    este capítulo. Como cualquiera puede hacer POST a una URL pública, el proveedor firma
    cada petición con un secreto compartido usando *HMAC* (un hash con clave); recalcular
    el hash sobre los bytes exactos recibidos y compararlo demuestra que la carga útil es
    genuina y no fue manipulada. *Idempotencia* significa "seguro de recibir más de
    una vez": los proveedores reintentan ante fallos de red, así que almacenas una clave de idempotencia e
    ignoras una repetición —de lo contrario una sola recarga con tarjeta podría acreditar un monedero dos veces—.

### WebhookEvent y AbstractWebhookEventListener

Cada evento entrante se modela como una dataclass `WebhookEvent`:

```python
@dataclass
class WebhookEvent:
    id: str               # auto-generated UUID
    source: str           # e.g. "payment-provider"
    event_type: str       # from body["type"]
    headers: dict[str, str]
    body: dict[str, Any]
    raw_body: bytes
    received_at: datetime
    idempotency_key: str | None
```

Subclasifica `AbstractWebhookEventListener` y establece `source` con el nombre
que pasarás a `WebhookProcessor.process()`:

::: listing lumen/webhooks/payment_listener.py | Listado 17.8 — Listener de webhook del proveedor de pagos
from pyfly.container import service
from pyfly.webhooks import AbstractWebhookEventListener, WebhookEvent


@service
class PaymentWebhookListener(AbstractWebhookEventListener):
    source = "payment-provider"

    def __init__(self, deposit_handler) -> None:
        self._handler = deposit_handler

    async def handle(self, event: WebhookEvent) -> None:
        if event.event_type == "payment_intent.succeeded":
            pi = event.body.get("data", {}).get("object", {})
            wallet_id = pi.get("metadata", {}).get("wallet_id")
            amount_minor = int(pi.get("amount_received", 0))
            currency_code = pi.get("currency", "EUR").upper()
            if wallet_id and amount_minor > 0:
                # Delegate to the CQRS command handler so the aggregate
                # enforces the balance invariant and raises FundsDeposited.
                from lumen.core.services.wallets.deposit_funds_command import (
                    DepositFunds,
                )
                await self._handler.handle(
                    DepositFunds(
                        wallet_id=wallet_id,
                        amount=amount_minor,
                    )
                )

    async def on_error(
        self, event: WebhookEvent, error: BaseException,
    ) -> None:
        # Override to DLQ or page on-call.
        pass
:::

`on_error` se invoca cuando `handle` lanza una excepción; por defecto es un no-op. Reescríbelo para publicar en una cola de mensajes muertos o emitir una métrica.

### WebhookProcessor: verificar, deduplicar, despachar

**`WebhookProcessor`** ensambla un validador de firmas, un almacén de idempotencia y una lista de listeners. Móntalo en una clase `@configuration` para que sea un
único bean compartido:

- `listeners` es la lista de subclases de `AbstractWebhookEventListener` a las que abrir en
  abanico los eventos (de momento solo `PaymentWebhookListener`);
- `signature_validators` asocia cada nombre de `source` con el validador que demuestra que sus
  peticiones son genuinas —aquí un `HmacSignatureValidator` con clave del secreto del webhook
  que te dio tu proveedor—;
- un `event_store` (omitido aquí, así que se usa el por defecto en memoria) recuerda
  las claves de idempotencia.

::: listing lumen/webhooks/processor_config.py | Listado 17.9 — Ensamblando WebhookProcessor
from pyfly.container import bean, configuration
from pyfly.webhooks import (
    HmacSignatureValidator,
    WebhookProcessor,
)
from lumen.webhooks.payment_listener import PaymentWebhookListener


@configuration
class WebhookConfig:

    @bean
    def webhook_processor(
        self, payment_listener: PaymentWebhookListener,
    ) -> WebhookProcessor:
        return WebhookProcessor(
            listeners=[payment_listener],
            signature_validators={
                "payment-provider": HmacSignatureValidator(
                    secret="whsec_REPLACE_ME",
                ),
            },
        )
:::

`HmacSignatureValidator` espera el formato de cabecera `sha256=<hex>` y usa `hmac.compare_digest` para una comparación en tiempo constante. Cambia el parámetro `header_prefix` si tu proveedor usa un esquema diferente.

### Manejando un webhook en un manejador HTTP

Llama a `processor.process()` desde tu manejador HTTP entrante. Pasa el cuerpo de la petición sin procesar (bytes sin modificar): el validador calcula el HMAC sobre los bytes exactos recibidos.

!!! warning "Lee el cuerpo como bytes en crudo, no como JSON parseado"
    La firma se calcula sobre los *bytes exactos* que envió el proveedor. Si
    parseas el JSON y lo vuelves a serializar, el orden de las claves o los espacios en blanco pueden cambiar y el
    HMAC ya no coincidirá —toda petición legítima sería rechazada—.
    Pasa siempre `await request.body()` (los bytes intactos) a `process()`, como
    hace el manejador de abajo.

::: listing lumen/webhooks/payment_handler.py | Listado 17.10 — Endpoint de webhook entrante del proveedor de pagos
from pyfly.container import rest_controller
from pyfly.web import post_mapping, request_mapping
from pyfly.webhooks import WebhookProcessor
from starlette.requests import Request
from starlette.responses import Response


@rest_controller
@request_mapping("/webhooks")
class PaymentWebhookHandler:

    def __init__(self, processor: WebhookProcessor) -> None:
        self._processor = processor

    @post_mapping("/payment")
    async def receive(self, request: Request) -> Response:
        # Read the untouched bytes — the HMAC is computed over exactly
        # what the provider sent (see the warning above).
        raw_body = await request.body()
        headers = {
            "X-Signature": request.headers.get(
                "X-Webhook-Signature", ""
            ),
            "X-Idempotency-Key": request.headers.get(
                "X-Idempotency-Key", ""
            ),
        }
        try:
            await self._processor.process(
                source="payment-provider",
                raw_body=raw_body,
                headers=headers,
            )
        except ValueError:
            return Response(content=b"invalid signature", status_code=400)
        return Response(content=b"ok", status_code=200)
:::

La firma de `process()` acepta los argumentos por palabra clave `signature_header` e `idempotency_header` para reemplazar los nombres de cabecera por defecto (`X-Signature` y `X-Idempotency-Key`).

**Qué ocurre dentro de `process()`:**

1. El validador se busca por `source`; si no hay ninguno registrado, se usa un `NoOpSignatureValidator` —que siempre pasa, seguro para desarrollo pero no para producción—.
2. Si la validación de la firma falla, se lanza `ValueError` de inmediato y no se invoca a ningún listener.
3. El cuerpo en crudo se decodifica como JSON; si falla, los bytes en crudo se almacenan bajo `body["_raw"]`.
4. Si `idempotency_key` está presente y ya se ha visto, el evento se devuelve pero **no** se invoca a los listeners.
5. Cada listener para el source se invoca en el orden de registro; si uno lanza una excepción, el error se registra y se invoca `on_error` antes de continuar con el siguiente listener.

**Ejecútalo.** Prueba el endpoint como lo haría un proveedor real: firmando el cuerpo
exacto. Elige el mismo secreto que pusiste en `HmacSignatureValidator` (`whsec_REPLACE_ME`
en el Listado 17.9), calcula el HMAC con `openssl` y haz POST. Inicia la aplicación
(`uv run pyfly run --server uvicorn`), luego en un segundo terminal:

::: listing terminal | Listado 17.10a — Firma y POST de un webhook
BODY='{"type":"payment_intent.succeeded","data":{"object":{"amount_received":25000,"currency":"eur","metadata":{"wallet_id":"<wallet-id>"}}}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "whsec_REPLACE_ME" | sed 's/^.* //')

curl -s -X POST localhost:8080/webhooks/payment \
  -H "X-Webhook-Signature: sha256=$SIG" \
  -H "X-Idempotency-Key: evt-001" \
  -H 'content-type: application/json' \
  -d "$BODY"
:::

Una petición correctamente firmada devuelve `ok` y acredita el monedero (verás también
dispararse los logs de notificación de `FundsDeposited` de antes):

```text
ok
```

Ahora demuestra las dos garantías. Haz POST del **mismo** comando otra vez con la misma
`X-Idempotency-Key`: sigue devolviendo `ok`, pero el monedero *no* se acredita una
segunda vez (el duplicado se descarta antes de que se ejecute ningún listener). Luego altera
un byte de `$BODY` *sin* recalcular `$SIG` y vuelve a hacer POST: la firma
ya no coincide, así que el manejador devuelve:

```text
invalid signature
```

Esa es la tubería verificar-deduplicar-despachar funcionando de extremo a extremo, y refleja
el Ejercicio 3 casi exactamente.

!!! note "Qué acaba de pasar"
    Un desconocido en internet hizo POST de JSON a tu servicio, y tres guardianes se ejecutaron
    antes que una sola línea de tu lógica de negocio: la comprobación de la firma rechazó
    falsificaciones, el almacén de idempotencia rechazó reintentos, y solo entonces el
    listener tipado tradujo el evento en un comando CQRS `DepositFunds` —así que
    la raíz de agregado del monedero siguió haciendo cumplir sus propios invariantes—. Tú escribiste el
    cuerpo de `handle()`; PyFly proporcionó el desafío que lo rodea.

!!! note "Almacén de idempotencia en memoria"
    El `InMemoryWebhookEventStore` por defecto guarda las claves vistas en un
    `set` de Python. Para clústeres de producción, implementa el protocolo
    `WebhookEventStore` respaldado por Redis o una base de datos:
    ```python
    class RedisEventStore:
        async def already_processed(
            self, key: str,
        ) -> bool: ...
        async def remember(self, key: str) -> None: ...
    ```
    Pásalo como `event_store=RedisEventStore(...)` a `WebhookProcessor`.

---

## Callbacks salientes

Cuando Lumen registra un desembolso a un banco socio, ese socio espera un POST `DisbursementSettled` a su URL de webhook —firmado, reintentado ante fallos y auditable—. El módulo `pyfly.callbacks` de PyFly se encarga del lado saliente.

!!! note "Término nuevo: callback saliente"
    Un *callback* aquí es lo inverso del webhook entrante que acabas de construir:
    ahora *Lumen* es el remitente, que hace POST de un evento *a* la URL de un socio. El
    módulo te da la misma maquinaria de confianza y fiabilidad en la salida:
    firma cada carga útil (para que el socio pueda verificarla), reintenta los fallos
    transitorios con retroceso (backoff) y registra cada intento para que tengas un rastro de auditoría
    cuando un socio pregunte "¿nos llegasteis a avisar de la transacción X?".

### Suscripciones y configuración

Cada socio se modela como una **`CallbackConfig`** —un registro con alcance de inquilino (tenant) que contiene el secreto del webhook, las suscripciones a eventos y la política de reintentos—.

!!! note "Término nuevo: inquilino (tenant)"
    Un *inquilino* (tenant) es un cliente u organización aislada dentro de una
    aplicación compartida —aquí, `"lumen"`—. Los callbacks tienen alcance de inquilino para que un
    despliegue multiinquilino pueda mantener las URLs de socios, secretos y política de reintentos
    de cada inquilino por separado y nunca cruzar los cables. Con un solo inquilino simplemente pasas
    el mismo `tenant_id` en todas partes.

**Paso 1 — Describe cada suscripción.** Una `CallbackSubscription` empareja un
`event_type` con la `target_url` a la que hacerle POST. Usa el nombre exacto del evento
(`"DisbursementSettled"`) para encaminar un evento, o `"*"` como comodín que
coincide con todos los eventos del inquilino —útil para un endpoint de auditoría que quiere
el flujo completo—.

**Paso 2 — Envuélvelas en una `CallbackConfig` y guárdala.** La configuración lleva el
`secret` compartido (usado para firmar cada carga útil), la política de reintentos (`max_attempts`,
`backoff_ms`) y la lista de suscripciones. Persístela a través de un
`CallbackConfigRepository` —`InMemoryCallbackConfigRepository` por ahora, uno
respaldado por base de datos en producción—.

::: listing lumen/callbacks/register_partner.py | Listado 17.11 — Registrando un callback de socio
from pyfly.callbacks import (
    CallbackConfig,
    CallbackSubscription,
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)


async def register_clearance_bank(configs) -> None:
    await configs.save(CallbackConfig(
        tenant_id="lumen",
        name="clearance-bank",
        secret="cb-secret-xyz",
        max_attempts=5,
        backoff_ms=2_000,
        subscriptions=[
            CallbackSubscription(
                event_type="DisbursementSettled",
                target_url=(
                    "https://api.clearancebank.example.com"
                    "/hooks/lumen"
                ),
            ),
            CallbackSubscription(
                event_type="*",
                target_url=(
                    "https://audit.clearancebank.example.com"
                    "/all-events"
                ),
            ),
        ],
    ))
:::

`event_type="*"` es un comodín: todos los eventos despachados para el inquilino coinciden. Los tipos con nombre solo coinciden con la cadena exacta del tipo de evento.

### Despachando un evento

**`CallbackDispatcher.dispatch()`** abre el evento en abanico hacia cada suscripción coincidente:

::: listing lumen/callbacks/dispatcher_config.py | Listado 17.12 — Cableando e invocando CallbackDispatcher
from pyfly.callbacks import (
    CallbackDispatcher,
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)
from pyfly.container import bean, configuration


@configuration
class CallbackConfig_:

    @bean
    def callback_configs(self) -> InMemoryCallbackConfigRepository:
        return InMemoryCallbackConfigRepository()

    @bean
    def callback_executions(
        self,
    ) -> InMemoryCallbackExecutionRepository:
        return InMemoryCallbackExecutionRepository()

    @bean
    def callback_dispatcher(
        self,
        configs: InMemoryCallbackConfigRepository,
        executions: InMemoryCallbackExecutionRepository,
    ) -> CallbackDispatcher:
        return CallbackDispatcher(
            configs=configs,
            executions=executions,
        )
:::

Luego, en el servicio de dominio —fíjate en que la carga útil usa `amount` en unidades menores (céntimos), así que `50_000` son EUR 500.00—:

```python
results = await dispatcher.dispatch(
    "lumen",            # tenant_id
    "DisbursementSettled",
    {"id": "txn-009", "amount": 50_000, "currency": "EUR"},
)
```

`dispatch()` devuelve un registro `CallbackExecution` por cada suscripción coincidente, cada uno con `status`, `attempts`, `response_status` y `delivered_at`.

**Ejecútalo.** El emisor HTTP por defecto del despachador no llama realmente a la
red —registra la petición que *haría* y devuelve `200`—, lo cual es
perfecto para ver el cableado sin levantar un servidor de socio. Suelta esto
en un script (o en `uv run python`) desde `samples/lumen`:

::: listing lumen/callbacks/try_dispatch.py | Listado 17.12a — Despacho contra el emisor por defecto (solo registra)
import asyncio

from pyfly.callbacks import (
    CallbackConfig,
    CallbackDispatcher,
    CallbackSubscription,
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)


async def main() -> None:
    configs = InMemoryCallbackConfigRepository()
    await configs.save(CallbackConfig(
        tenant_id="lumen",
        name="clearance-bank",
        secret="cb-secret-xyz",
        subscriptions=[CallbackSubscription(
            event_type="DisbursementSettled",
            target_url="https://api.clearancebank.example.com/hooks/lumen",
        )],
    ))
    dispatcher = CallbackDispatcher(
        configs=configs,
        executions=InMemoryCallbackExecutionRepository(),
    )
    results = await dispatcher.dispatch(
        "lumen",
        "DisbursementSettled",
        {"id": "txn-009", "amount": 50_000, "currency": "EUR"},
    )
    for r in results:
        print(r.status, r.attempts, r.response_status, r.target_url)


asyncio.run(main())
:::

Deberías ver una ejecución entregada, con la petición firmada registrada justo
encima de ella:

```text
would POST https://api.clearancebank.example.com/hooks/lumen headers={'X-Pyfly-Signature': 'sha256=...', 'Content-Type': 'application/json'} body={'id': 'txn-009', 'amount': 50000, 'currency': 'EUR'}
DELIVERED 1 200 https://api.clearancebank.example.com/hooks/lumen
```

`DELIVERED 1 200` se lee como: estado `DELIVERED`, con éxito en el intento `1`, HTTP
`200`. Para enviar de verdad, pasa tu propio emisor `http=` (un POST de `httpx`/`aiohttp`)
a `CallbackDispatcher`; la lógica de firma, reintento y auditoría se mantiene idéntica.

!!! note "Qué acaba de pasar"
    Una sola llamada a `dispatch()` buscó todas las suscripciones que el inquilino tiene para ese
    tipo de evento, firmó la carga útil, hizo POST y escribió un registro `CallbackExecution`
    para cada una —todo ello sin que tu servicio de dominio supiera cuántos socios
    están escuchando ni cómo funcionan los reintentos—. Añadir un socio más adelante es solo otra
    `CallbackConfig` guardada; el código de desembolso nunca cambia.

### Firma HMAC y lógica de reintentos

Cuando `CallbackConfig.secret` está establecido, `CallbackDispatcher` firma la carga útil JSON canónica antes de cada POST usando HMAC-SHA256:

```python
# canonical body — compact, keys sorted
canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
sig = hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()
headers["X-Pyfly-Signature"] = f"sha256={sig}"
```

El destinatario puede verificar la firma usando el propio `HmacSignatureValidator` de PyFly —la misma clase usada para los webhooks entrantes—.

**Política de reintentos:**

- El despachador reintenta hasta `max_attempts` veces (por defecto `5`).
- Entre reintentos aplica retroceso exponencial: `delay = min(backoff_ms * 2^(attempt-1), 300_000) ms`.
- Solo los códigos de estado HTTP *transitorios* disparan un reintento: `408`, `429`, `500`, `502`, `503`, `504`, o cualquiera `>= 500`.
- Los errores de cliente permanentes (`4xx` salvo `408`/`429`) marcan la ejecución como `FAILED` de inmediato sin reintentar.
- Ante el éxito (`2xx`) la ejecución se marca como `DELIVERED` y se estampa `delivered_at`.

::: listing lumen/callbacks/models_overview.py | Listado 17.13 — Ciclo de vida del estado de CallbackExecution
from pyfly.callbacks import CallbackStatus

# After a successful delivery:
assert execution.status == CallbackStatus.DELIVERED
assert execution.delivered_at is not None
assert execution.response_status == 200

# After all retries are exhausted:
assert execution.status == CallbackStatus.FAILED
assert execution.attempts == 5
assert execution.last_error is not None
:::

### Protección contra SSRF: dominios autorizados

El campo `authorized_domains` de `CallbackConfig` actúa como una lista de permitidos. Cuando está establecido, `CallbackDispatcher` comprueba que el nombre de host de la URL de destino coincide con uno de los dominios permitidos antes de realizar cualquier petición saliente. Una URL que no supera la comprobación se marca de inmediato como `FAILED` con `last_error="Domain not authorized"` —no se realiza ninguna petición HTTP—.

```python
from pyfly.callbacks import AuthorizedDomain, CallbackConfig

config = CallbackConfig(
    tenant_id="lumen",
    name="safe-config",
    secret="s3cr3t",
    authorized_domains=[
        AuthorizedDomain(domain="clearancebank.example.com"),
    ],
    subscriptions=[...],
)
```

Los subdominios de los dominios permitidos también se aceptan (p. ej. `api.clearancebank.example.com`).

!!! note "Término nuevo: SSRF"
    La *falsificación de peticiones del lado del servidor* (Server-Side Request Forgery) es un ataque en el que un valor malicioso engaña a
    tu servidor para que haga una petición HTTP que no debería —por ejemplo, una
    URL de socio que apunta a `http://169.254.169.254/` (un endpoint de metadatos
    de la nube) para robar credenciales—. Como las URLs de callback pueden venir de
    configuración proporcionada por el socio, la lista de permitidos `authorized_domains` cierra esa puerta:
    un host que no está en la lista se marca como `FAILED` *antes* de que ninguna petición
    salga del proceso. Para verificarlo, añade un `AuthorizedDomain` para un host, luego
    despacha una suscripción cuya `target_url` apunte a otro sitio: el
    `CallbackExecution` devuelto mostrará `status=FAILED`, `attempts=0` y
    `last_error="Domain not authorized"`, y no se enviará nada.

!!! spring "Equivalencia con Spring"
    El trío `@scheduled` / `CronExpression` / `TaskScheduler` de PyFly
    refleja el `@Scheduled` / `CronExpression` /
    `ThreadPoolTaskScheduler` de Spring. Los puertos de notificación se corresponden con los
    `MailSender` / `JavaMailSender` de Spring. `WebhookProcessor` se corresponde con
    un `@RestController` de Spring + el `HmacRequestMatcher` de HMAC de Spring
    Security. `CallbackDispatcher` con firma HMAC y
    reintentos refleja el patrón `WebhookPublisher` de Spring
    Modulith.

---

**Ejecútalo — confirma que la suite sigue en verde.** Añadiste cuatro nuevos patrones
de integración; asegúrate de que nada más sufrió una regresión. Desde el directorio `samples/lumen`:

```bash
uv run --extra dev pytest -q
```

Deberías ver que todas las pruebas existentes siguen pasando:

```text
.........................................                                [100%]
41 passed in 0.28s
```

Los tres ejercicios de abajo añaden sus propias pruebas de programación, notificación y webhook —
vuelve a ejecutar este comando después de cada uno para ver subir el recuento—. Recuerda la
bandera `--extra dev`; el `uv sync` a secas omite pytest.

---

## Lo que construiste {.recap}

Extendiste Lumen hasta convertirlo en un sistema conectado que opera con independencia de las peticiones entrantes:

- **Tareas programadas** — `@scheduled` con los disparadores `cron`, `fixed_rate` y `fixed_delay` ejecuta trabajo según un calendario o un temporizador. `CronExpression` impulsa los cálculos de los momentos de disparo, incluida la programación consciente de la zona horaria con el horario de verano gestionado automáticamente. `lock=True` serializa la ejecución en todo el clúster a través del puerto `DistributedLock`. `@async_method` descarga el trabajo fire-and-forget en el ejecutor.

- **Notificaciones** — Los protocolos de puerto `EmailService`, `SmsService` y `PushService` desacoplan la lógica de negocio de los adaptadores de proveedor (SMTP, SendGrid, Resend, Twilio, Firebase). `DefaultEmailService` y sus hermanos capturan los errores del proveedor y devuelven valores `NotificationResult` estructurados en lugar de propagar excepciones. `DepositNotificationListener` se suscribe al evento de EDA real `FundsDeposited` y convierte los importes en unidades menores en cadenas de visualización antes de enviar.

- **Webhooks entrantes** — `AbstractWebhookEventListener` define consumidores tipados. `WebhookProcessor` filtra cada evento con `HmacSignatureValidator` y `WebhookEventStore` antes de despacharlo a los listeners. Los ganchos `on_error()` permiten la integración con una cola de mensajes muertos sin romper el bucle de despacho.

- **Callbacks salientes** — `CallbackDispatcher` abre los eventos en abanico hacia los destinos de `CallbackSubscription`, firma las cargas útiles con HMAC-SHA256 bajo la cabecera `X-Pyfly-Signature` y reintenta ante fallos transitorios con retroceso exponencial. Los registros `CallbackExecution` proporcionan un rastro de auditoría de entrega completo. `authorized_domains` previene la SSRF.

---

## Pruébalo tú mismo {.exercises}

1. **Bloqueo de clúster.** Añade una segunda instancia de `DailyRollupService` a una
   prueba que levante dos instancias de `ApplicationContext` compartiendo un
   `FakeDistributedLock` (uno que solo devuelva `True` para el primer
   adquirente). Afirma que `run()` se invoca exactamente una vez en ambas
   instancias para un único tic de cron.

2. **Cambio de proveedor.** Reemplaza `SmtpEmailProvider` por un
   `DummyEmailProvider` en la suite de pruebas. Escribe una prueba que deposite
   EUR 100 (10 000 unidades menores) en el monedero `w-001` a través del manejador
   de depósitos, disparando un evento `FundsDeposited`. El proveedor registra
   cada mensaje que recibió en su lista `.sent`, así que afirma que
   `provider.sent[-1].body_text` contiene el ID del monedero y el
   importe formateado (`100.00 EUR`).

3. **Ataque de reenvío de firma.** Escribe una prueba que invoque
   `PaymentWebhookHandler.receive()` dos veces con el mismo cuerpo en crudo,
   cabeceras y clave de idempotencia. Afirma que la primera llamada devuelve 200
   y acredita el monedero (disparando `FundsDeposited`), y que la segunda
   llamada también devuelve 200 (el duplicado lo ignora silenciosamente
   `InMemoryWebhookEventStore`) pero **no** acredita el monedero una
   segunda vez.
