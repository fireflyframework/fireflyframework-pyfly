<span class="eyebrow">Capítulo 11</span>

# Dividir el monolito: clientes HTTP y el BFF {.chtitle}

::: figure art/openers/ch11.svg | &nbsp;

Cuando Lumen era un único servicio, cada capacidad vivía en el mismo
proceso. El monedero, la comprobación de saldo y el procesamiento de
pagos se ejecutaban juntos: sencillos de probar, simples de desplegar y
perfectamente adecuados hasta que el equipo necesitó publicar
funcionalidades a ritmos distintos. Entonces llegó la conversación difícil
sobre la división.

La promesa de una división en microservicios es real: los equipos son
dueños de sus servicios de forma independiente, los escalan por separado
y los despliegan sin coordinar una ventana de publicación compartida. Pero
toda división introduce un problema que el monolito nunca tuvo: la red. Lo
que era una llamada a una función local se convierte en una petición HTTP
que puede agotar su tiempo de espera, fallar a medias o aterrizar en un
servicio saturado. Esa frontera de red no es un detalle de despliegue; es
una preocupación de ingeniería de primer orden.

Este capítulo presenta `PaymentsService`, un segundo servicio al que el
servicio Wallet de Lumen llama para liquidar transferencias. En lugar de
montar a mano sesiones de `httpx` e ir hilvanando la lógica del
cortacircuitos (circuit breaker) por cada manejador, definirás el cliente
de Payments como una clase de Python corriente: una interfaz tipada y
declarativa que PyFly rellena en el arranque. Al final del capítulo verás
también cómo un nivel **BFF (Backend for Frontend)** se sitúa por delante
de ambos servicios y compone sus capacidades en una única API centrada en
el recorrido del usuario.

!!! note "Nueva jerga, en lenguaje llano"
    Algunos términos se repiten a lo largo de este capítulo. **Llamada
    servicio a servicio** significa que uno de tus servicios hace una
    petición HTTP a otro de tus servicios (Wallet llamando a Payments), en
    contraste con una petición que llega desde un navegador. Un **cliente
    declarativo** es un cliente que *describes* en vez de *implementar*:
    escribes las firmas de los métodos y dejas que el framework rellene la
    fontanería HTTP. Un **cortacircuitos (circuit breaker)** es un
    interruptor de seguridad que deja de llamar a un servicio remoto
    después de que haya fallado demasiadas veces seguidas. Un **BFF**
    (Backend for Frontend) es un servicio fino cuya única misión es llamar
    a otros servicios y remodelar sus respuestas para una aplicación
    concreta. Construiremos cada una de estas piezas, una a una.

Este capítulo se articula en torno a `httpx` y al paquete `pyfly.client`,
ambos incluidos con el framework. Todo lo que hay aquí se ejecuta contra
PyFly v26.6.110. No necesitas instalar nada nuevo: si has ido siguiendo el
hilo con Lumen, las herramientas de cliente ya están en tu path.

---

## Por qué dividir (y por qué duele)

### La zona de confort del monolito

Un monolito no es un error arquitectónico: es un punto de partida
arquitectónico. Lumen comenzó como un único servicio porque un único
servicio era lo correcto: un equipo, una canalización de despliegue, un
conjunto de preocupaciones que razonar. La transacción de base de datos
que escribe una fila del monedero y publica un evento de dominio en la
misma unidad de trabajo no era un compromiso a la baja; era la elección
óptima.

La presión para dividir suele llegar desde fuera de la arquitectura.
Payments necesita un registro de auditoría de cumplimiento separado. La
puntuación de riesgo necesita un equipo especialista con acceso a una
fuente de datos privada. El procesamiento de liquidaciones exige un
rendimiento un orden de magnitud mayor que las lecturas de saldo.
Cualquiera de estas razones es buena para extraer un servicio, y ninguna
de ellas borra el hecho de que el resto del sistema todavía necesita
llamar al servicio extraído a través de una frontera de red.

### El coste de la red

Las llamadas de red fallan de maneras en que las llamadas a funciones
locales no lo hacen. Un método sobre un objeto local o bien devuelve un
valor o bien lanza una excepción. Una llamada HTTP a un servicio remoto
puede agotar su tiempo de espera (el remoto va lento), rechazar la
conexión (el remoto está caído), devolver un 503 transitorio (el remoto
está sobrecargado) o tener éxito solo al tercer intento. En un monolito
estos modos de fallo son irrelevantes; en un sistema distribuido son tu
línea de base.

El arreglo ingenuo —usar `httpx` directamente con `try/except` alrededor
de cada llamada— funciona para un único punto de llamada, pero no escala.
Acabas con la lógica del cortacircuitos duplicada en cada cliente de
servicio, retardos de reintento incrustados a fuego en los manejadores y
valores de tiempo de espera dispersos por fragmentos de `pyfly.yaml` que
nadie posee. Cuando Payments introduce un nuevo endpoint, cada quien que
llame debe acordarse de añadir de nuevo todo el andamiaje de resiliencia.

El cliente HTTP tipado de PyFly elimina esa duplicación. Declaras cómo es
el servicio remoto: sus endpoints, sus rutas y la forma de sus parámetros.
PyFly genera la implementación en el arranque, conecta un cortacircuitos y
una política de reintentos a partir de `pyfly.yaml` y registra el bean en
el contenedor para que cualquier manejador que lo necesite pueda
declararlo como argumento del constructor. La resiliencia se aplica una
vez, de forma coherente, en la capa correcta.

---

## Un cliente de servicio tipado

### Declarativo en vez de imperativo

La idea central del módulo de cliente de PyFly es que los contratos
servicio a servicio se expresan mejor como tipos que como lógica HTTP
procedimental. Cuando describes `PaymentsClient` como una clase con firmas
de método tipadas, obtienes una interfaz de Python que cualquier IDE puede
navegar, que cualquier verificador de tipos puede comprobar y que
cualquier prueba puede simular, sin importar nunca `httpx` en el código que
la usa.

Dos decoradores definen ese contrato:

| Decorador | Resiliencia incorporada | Úsalo para |
|---|---|---|
| `@service_client` | Cortacircuitos + reintentos | Llamadas servicio a servicio en producción |
| `@http_client` | Ninguna | Clientes ligeros, pruebas, utilidades internas |

Usa **`@service_client`** siempre que el destino sea otro microservicio.
Reserva `@http_client` para utilidades internas y dobles de prueba.

### Definir el cliente de Payments

El servicio Payments expone dos endpoints: uno para crear una instrucción
de pago y otro para recuperar un pago por identificador. Definir el cliente
consiste en escribir la clase. Construyámosla decisión a decisión.

**Paso 1 — Crea el archivo.** Añade `src/lumen/sdk/payments_client.py`
junto al `client.py` existente. El paquete `sdk` es donde Lumen guarda el
código que *habla con* servicios, así que este es el hogar natural para un
cliente de servicio.

**Paso 2 — Decora la clase.** Coloca `@service_client(base_url=...)` sobre
una clase normal. Ese único decorador es lo que convierte una clase
corriente en un cliente HTTP gestionado por PyFly: registra la URL base,
activa las funciones de resiliencia y registra la clase como un bean para
que el contenedor pueda inyectarla después.

**Paso 3 — Declara un método por endpoint.** Cada método recibe un
decorador de verbo (`@post`, `@get`, `@patch`, `@delete`) que lleva la
ruta. El cuerpo del método es solo `...` —una *elipsis*, el literal de
Python para "aquí todavía no hay nada"—. Nunca escribes el código de la
petición; PyFly lo escribe por ti en el arranque.

El cliente completo tiene este aspecto.

::: figure art/figures/11-client.svg | Figura 11.1 — La canalización del cliente declarativo de PyFly. Tú escribes la interfaz; HttpClientBeanPostProcessor genera la implementación.

::: listing lumen/sdk/payments_client.py | Listado 11.1 — Cliente de Payments tipado con @service_client
from __future__ import annotations

from pyfly.client import (
    delete,
    get,
    patch,
    post,
    service_client,
)


@service_client(
    base_url="http://payments-service:8080",
    circuit_breaker=True,
    retry=3,
    circuit_breaker_failure_threshold=5,
    circuit_breaker_recovery_timeout=60.0,
    retry_base_delay=1.0,
)
class PaymentsClient:
    """Typed HTTP client for the Payments service.

    Method stubs are replaced with real HTTP implementations by
    HttpClientBeanPostProcessor at application startup. Declare
    this class as a constructor argument to have it injected.
    """

    @post("/payments")
    async def create_payment(self, body: dict) -> dict:
        """POST /payments — submit a payment instruction."""
        ...

    @get("/payments/{payment_id}")
    async def get_payment(self, payment_id: str) -> dict:
        """GET /payments/:payment_id — fetch a payment by ID."""
        ...

    @patch("/payments/{payment_id}/cancel")
    async def cancel_payment(self, payment_id: str) -> dict:
        """PATCH /payments/:payment_id/cancel — cancel pending."""
        ...

    @delete("/payments/{payment_id}")
    async def delete_payment(self, payment_id: str) -> None:
        """DELETE /payments/:payment_id — remove a completed record."""
        ...
:::

**Cómo funciona — la canalización de declaración:**

`@service_client(base_url=...)` estampa atributos de metadatos sobre la
clase y la registra como un bean singleton en el contenedor de PyFly: el
mismo mecanismo `__pyfly_injectable__ = True` que usa `@service`. La
`base_url` se almacena como `__pyfly_http_base_url__`; las opciones de
resiliencia aterrizan en `__pyfly_resilience__`.

Los decoradores de verbo —`@post("/payments")`,
`@get("/payments/{payment_id}")` y los demás— adjuntan cada uno dos
atributos a su método: `__pyfly_http_method__` (la cadena con el verbo
HTTP) y `__pyfly_http_path__` (la plantilla de ruta). El cuerpo del método
en sí se convierte en un stub que lanza `NotImplementedError` y nunca
debería llamarse directamente.

En el arranque, `HttpClientBeanPostProcessor.after_init()` inspecciona cada
bean. Cuando encuentra una clase con `__pyfly_http_client__ = True`, crea
un `HttpxClientAdapter` para la `base_url`, recorre cada método buscando
`__pyfly_http_method__` y reemplaza cada stub por una implementación
asíncrona real. Esa implementación usa `inspect.signature()` para enlazar
los argumentos de quien llama, interpola las variables de ruta
(`{payment_id}` → el valor real), separa los parámetros restantes en
cadenas de consulta o un cuerpo JSON, y llama a `client.request()`. Las
respuestas con estado ≥ 400 lanzan excepciones tipadas; las respuestas
correctas devuelven `response.json()`.

La interpolación de variables de ruta es posicional: cualquier parámetro
cuyo nombre coincida con un `{marcador}` de la plantilla de ruta es
sustituido. Para `get_payment(self, payment_id: str)`, llamar a
`client.get_payment("pay-123")` envía `GET /payments/pay-123`. Para
`create_payment(self, body: dict)`, llamar a
`client.create_payment({"amount": 5000})` envía `POST /payments` con el
dict serializado como cuerpo JSON. Los parámetros llamados `body` en
métodos POST/PUT/PATCH siempre se tratan como el cuerpo JSON de la
petición; todos los demás parámetros que no son de ruta en métodos
GET/DELETE se convierten en parámetros de cadena de consulta.

!!! note "Qué acaba de pasar"
    Escribiste cuatro *firmas* de método y cero líneas de código HTTP. El
    decorador `@service_client` etiquetó la clase para el contenedor; los
    decoradores de verbo etiquetaron cada método con un verbo y una ruta.
    En el arranque, un componente entre bambalinas llamado
    `HttpClientBeanPostProcessor` lee esas etiquetas y reemplaza
    silenciosamente cada stub `...` por un método asíncrono funcional que
    construye la URL, envía la petición y vuelve a convertir la respuesta
    en Python. Desde el punto de vista de quien llama,
    `await client.get_payment("pay-123")` se ve exactamente igual que
    llamar a un método local: la red queda oculta.

**Ejecútalo — confirma que los stubs están conectados.** Hasta que el
contexto de aplicación arranca, esos cuerpos `...` lanzan
`NotImplementedError` a propósito, así que no puedes simplemente llamar al
método en un script pelado. La forma honesta de demostrar que el cliente
funciona es una prueba diminuta que arranque un contexto (o conecte el
post-procesador) e inspeccione el método generado. La comprobación rápida
más sencilla es confirmar los metadatos que los decoradores estamparon:

```
uv run python -c "from lumen.sdk.payments_client import PaymentsClient; \
print(PaymentsClient.__pyfly_http_base_url__); \
print(PaymentsClient.create_payment.__pyfly_http_method__, \
PaymentsClient.create_payment.__pyfly_http_path__)"
```

Salida esperada:

```
http://payments-service:8080
POST /payments
```

Si ves la URL base y `POST /payments`, los decoradores se aplicaron
correctamente y el post-procesador tiene todo lo que necesita para generar
la implementación real cuando la aplicación arranque.

!!! spring "Equivalencia con Spring"
    `@service_client` con `@get`/`@post`/`@put`/`@delete`/`@patch` es el
    homólogo en PyFly de `@FeignClient` de Spring Cloud OpenFeign con
    `@GetMapping`/`@PostMapping`, etc. En Feign anotas una interfaz; en
    PyFly anotas una clase con métodos stub: la intención es idéntica.
    Ambos frameworks generan la implementación HTTP en tiempo de arranque,
    inyectan el bean a través del contenedor de inyección de dependencias y
    admiten cortacircuitos (Feign vía Resilience4j; PyFly vía el
    `CircuitBreaker` incorporado). La diferencia clave es que Feign trabaja
    sobre interfaces de Java mientras que PyFly trabaja sobre clases de
    Python corrientes, lo que significa que puedes añadir métodos
    auxiliares junto a los métodos stub: útil para la lógica de remodelado
    de respuestas que pertenece dentro de la propia clase cliente.

### Inyectar el cliente en un manejador

Como `PaymentsClient` es un bean singleton, cualquier `@service` o
`@command_handler` puede declararlo como argumento del constructor. El
contenedor de PyFly lo inyecta a través de la misma vía de autoconexión
usada para repositorios y servicios de dominio.

!!! note "Bean y autoconexión, en breve"
    Un **bean** es simplemente un objeto que el framework crea y gestiona
    por ti: nunca llamas tú a su constructor. La **autoconexión** es cómo
    el contenedor entrega un bean a quien lo necesite: cuando una clase
    enumera `payments: PaymentsClient` en su `__init__`, PyFly se percata
    del tipo, encuentra el bean coincidente y lo pasa automáticamente.
    Declaras la dependencia por *tipo*; el contenedor hace la búsqueda.

El servicio Wallet de Lumen ya aplica el patrón del `WalletRepository` y el
objeto de valor `Money` de capítulos anteriores. Cuando el monedero debe
llamar a Payments para liquidar un reintegro, el manejador sigue ese mismo
patrón en tres pasos: cargar el monedero, retirar a través de la raíz de
agregado y luego llamar al servicio externo.

**Paso 1 — Declara la dependencia.** Añade `payments: PaymentsClient` al
constructor del manejador y guárdalo en `self`. Esa es la única conexión
que escribes; el contenedor suministra el cliente en vivo.

**Paso 2 — Haz primero el trabajo local.** Carga el monedero y llama a
`wallet.withdraw(...)`, después persístelo, de modo que el propio estado
del monedero quede liquidado antes de que ocurra cualquier llamada de red.

**Paso 3 — Llama al servicio remoto.**
`await self._payments.create_payment(...)` se lee como una llamada a un
método local. Los detalles de resiliencia y HTTP ya vienen horneados en el
cliente inyectado.

Aquí está el manejador.

::: listing lumen/core/services/wallets/settle_transfer_handler.py | Listado 11.2 — CommandHandler que inyecta PaymentsClient
from __future__ import annotations

from lumen.core.services.wallets.settle_transfer_command import (
    SettleTransfer,
)
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from lumen.sdk.payments_client import PaymentsClient
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound


@command_handler
@service
class SettleTransferHandler(CommandHandler[SettleTransfer, dict]):
    """Withdraw from the wallet and submit a payment instruction."""

    def __init__(
        self,
        repository: WalletRepository,
        payments: PaymentsClient,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._payments = payments

    async def do_handle(self, command: SettleTransfer) -> dict:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.withdraw(
            Money(amount=command.amount, currency=wallet.currency)
        )
        await self._repository.add(wallet)

        payment = await self._payments.create_payment({
            "wallet_id": command.wallet_id,
            "amount": command.amount,
            "currency": wallet.currency.value,
            "reference": command.reference,
        })
        return payment
:::

**Cómo funciona — la vía de inyección:**

`payments: PaymentsClient` en el constructor es resuelto por el contenedor
en el arranque. `HttpClientBeanPostProcessor` conecta `PaymentsClient`
antes de que se instancie `SettleTransferHandler`, de modo que el bean
inyectado está plenamente operativo. El manejador llama a
`await self._payments.create_payment(...)` exactamente como si fuera un
método asíncrono local. La agrupación de conexiones (connection pooling),
la propagación de cabeceras y el mapeo de errores son todos invisibles
para el manejador.

`wallet.withdraw(Money(...))` se ejecuta antes de la llamada de red, así
que el estado del monedero queda confirmado antes de que se contacte a
Payments. Si Payments está temporalmente no disponible, los reintentos y el
cortacircuitos —descritos en la siguiente sección— gestionan la
recuperación de forma transparente, sin ningún código en el manejador.

**Ejecútalo — ejercita el manejador con un cliente falso.** Como el
manejador depende del *tipo* `PaymentsClient`, puedes sustituirlo por un
suplente en una prueba sin ninguna red. Pon esto en
`tests/test_settle_transfer.py` y ejecútalo:

```
uv run --extra dev pytest tests/test_settle_transfer.py -q
```

Una ejecución exitosa imprime algo como:

```
1 passed in 0.12s
```

La gracia de la prueba es la sustitución: pasas un objeto hecho a mano en
lugar del `PaymentsClient` real, afirmas que el manejador llamó a
`create_payment` con el cuerpo esperado y nunca abres un socket. Esa es la
recompensa práctica de declarar la dependencia por tipo: al manejador ni le
consta ni le importa si el cliente del otro lado es real o falseado.

---

## Resiliencia sobre el cable

### Por qué la capa de cliente es el lugar correcto para la resiliencia

La lógica de resiliencia dentro de un manejador mezcla preocupaciones de
negocio con fontanería de infraestructura. Un manejador que captura
`httpx.ConnectError` e implementa su propio bucle de espera está haciendo
dos cosas a la vez: liquidar una transferencia *y* gestionar los modos de
fallo de HTTP. Esas responsabilidades pertenecen a capas separadas.

**`@service_client`** mueve el **cortacircuitos** y la **política de
reintentos** a la capa de cliente, donde corresponde. Los configuras una
vez en el decorador y cada método del cliente los hereda de manera
uniforme. El código del manejador permanece centrado en la operación de
negocio.

### Cortacircuitos

Un cortacircuitos monitoriza cada llamada al servicio remoto. Cuando
`failure_threshold` llamadas consecutivas fallan, el circuito se **abre**:
las llamadas posteriores se rechazan de inmediato con
`CircuitBreakerException` en lugar de esperar a un agotamiento de tiempo de
espera. Esto evita que un único servicio lento o no disponible bloquee el
bucle de eventos y agote las agrupaciones de conexiones de todos los
llamantes.

Tras `circuit_breaker_recovery_timeout` segundos, el circuito entra en
**medio abierto**: se admite una petición de sondeo. Si tiene éxito, el
circuito se cierra y se reanuda la operación normal. Si falla, el circuito
vuelve a abrirse y se reinicia el temporizador de recuperación.

`@service_client` conecta el cortacircuitos automáticamente. Si lo
necesitas por separado:

::: listing lumen/sdk/standalone_breaker.py | Listado 11.3 — Uso de CircuitBreaker por separado
from __future__ import annotations

from datetime import timedelta

from pyfly.client import CircuitBreaker
from pyfly.kernel.exceptions import CircuitBreakerException


breaker = CircuitBreaker(
    failure_threshold=3,
    recovery_timeout=timedelta(seconds=30),
)


async def call_with_breaker(client, payment_id: str) -> dict:
    """Fetch a payment through a standalone circuit breaker."""
    try:
        return await breaker.call(
            client.get_payment,
            payment_id,
        )
    except CircuitBreakerException:
        return {"status": "unavailable", "payment_id": payment_id}
:::

**Cómo funciona:** `CircuitBreaker.__init__` acepta `failure_threshold`
(por defecto `5`) y `recovery_timeout` como un `timedelta` (por defecto
30 s). `breaker.call(func, *args)` ejecuta `func(*args)` dentro del
cortacircuitos: si tiene éxito, reinicia la cuenta de fallos; si falla,
incrementa la cuenta y voltea el estado a `OPEN` una vez que se alcanza el
umbral. Las transiciones de estado `CLOSED → HALF_OPEN` se calculan de
forma perezosa con `time.monotonic()`: no hay temporizador en segundo
plano.

`CircuitBreakerException` nunca se contabiliza como un fallo. Señala que el
circuito ya está abierto, así que volver a lanzarla sin registrar otro
fallo impide que el tiempo de espera de recuperación se reinicie
indefinidamente.

!!! note "Tres estados, en lenguaje llano"
    Piensa en el cortacircuitos como un interruptor de luz con tres
    posiciones. **Cerrado** es lo normal: las llamadas fluyen a través.
    **Abierto** significa "deja de intentarlo": las llamadas fallan al
    instante sin tocar la red, lo que libra a tu servicio de esperar por
    algo que claramente está caído. **Medio abierto** es "déjame tantear el
    terreno": tras el tiempo de espera de recuperación, el cortacircuitos
    deja pasar exactamente una llamada; si funciona, el interruptor vuelve
    a cerrado, y si falla, salta de nuevo a abierto.

**Ejecútalo — observa cómo un cortacircuitos se abre y se recupera.**
Puedes accionar el cortacircuitos a mano desde un REPL sin ningún servicio
real involucrado. Arranca uno con `uv run python` desde `samples/lumen` y
prueba:

```
uv run python -c "
import asyncio
from datetime import timedelta
from pyfly.client import CircuitBreaker, CircuitState
from pyfly.kernel.exceptions import CircuitBreakerException

async def boom():
    raise RuntimeError('payments down')

async def main():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=timedelta(seconds=30))
    for _ in range(2):
        try:
            await cb.call(boom)
        except RuntimeError:
            pass
    print('state after 2 failures:', cb.state.name)
    try:
        await cb.call(boom)
    except CircuitBreakerException:
        print('open circuit rejected the call without calling boom')

asyncio.run(main())
"
```

Salida esperada:

```
state after 2 failures: OPEN
open circuit rejected the call without calling boom
```

La segunda línea es justo el quid: una vez que el circuito está abierto, el
cortacircuitos lanza `CircuitBreakerException` *de inmediato* en lugar de
ejecutar `boom` de nuevo. Baja `recovery_timeout` a `timedelta(seconds=0)`
y vuelve a ejecutarlo: la siguiente lectura de `cb.state` reporta
`HALF_OPEN` y la tercera llamada admite un sondeo (así que `boom` se
ejecuta otra vez) en vez de ser rechazada. Eso es el cortacircuitos
dejando que el servicio demuestre que se ha recuperado.

### Política de reintentos

Los fallos transitorios —un pico momentáneo de latencia, un reinicio
progresivo, un breve reinicio de conexión— no necesitan un cortacircuitos;
necesitan un segundo intento. `RetryPolicy` proporciona espera exponencial
con filtrado de excepciones configurable:

::: listing lumen/sdk/standalone_retry.py | Listado 11.4 — Uso de RetryPolicy por separado
from __future__ import annotations

from datetime import timedelta

from pyfly.client import RetryPolicy


policy = RetryPolicy(
    max_attempts=3,
    base_delay=timedelta(milliseconds=500),
    retry_on=(ConnectionError, TimeoutError),
)


async def resilient_fetch(client, payment_id: str) -> dict:
    """Fetch a payment with retry on transient network errors."""
    return await policy.execute(
        client.get_payment,
        payment_id,
    )
:::

**Cómo funciona:** `RetryPolicy.__init__` acepta `max_attempts` (por
defecto 3, contando el primer intento), `base_delay` (por defecto 1 s) y
`retry_on`, una tupla de tipos de excepción. La fórmula de espera es
`base_delay * (2 ** attempt)`: para `base_delay=0.5 s`, los retardos son
0,5 s, 1 s, 2 s. Solo las excepciones que coinciden con `retry_on`
disparan un reintento; las demás se propagan de inmediato. Esto importa: no
quieres reintentar un 404 (el recurso no existe) ni un 422 (la petición es
semánticamente inválida).

!!! note "Espera (backoff), en lenguaje llano"
    *Espera (backoff)* significa aguardar un poco más antes de cada
    reintento en vez de aporrear el servicio remoto en el instante en que
    falla. La espera *exponencial* duplica la espera cada vez, así que un
    servicio que necesita un momento para recuperarse obtiene más oxígeno
    con cada intento, mientras que uno sano se reintenta casi de inmediato.

**Ejecútalo — observa cómo el reintento se recupera de un error
transitorio.** Simula una llamada que falla dos veces y luego tiene éxito:

```
uv run python -c "
import asyncio
from datetime import timedelta
from pyfly.client import RetryPolicy

attempts = {'n': 0}
async def flaky():
    attempts['n'] += 1
    if attempts['n'] < 3:
        raise ConnectionError('reset')
    return 'ok'

async def main():
    policy = RetryPolicy(max_attempts=3, base_delay=timedelta(milliseconds=1),
                         retry_on=(ConnectionError,))
    print('result:', await policy.execute(flaky))
    print('attempts:', attempts['n'])

asyncio.run(main())
"
```

Salida esperada:

```
result: ok
attempts: 3
```

Tres intentos, un éxito. Cambia `retry_on` a `(TimeoutError,)` y el primer
`ConnectionError` se propaga en su lugar: prueba de que solo se reintentan
las excepciones que enumeras.

Cuando `@service_client` activa ambas funciones, el post-procesador las
envuelve en el orden correcto: cortacircuitos *fuera*, reintento *dentro*.
Una única llamada lógica intenta hasta `max_attempts` reintentos antes de
que el cortacircuitos registre un fallo. Un circuito abierto rechaza la
llamada de inmediato, saltándose por completo el bucle de reintentos.

### Excepciones de error tipadas

Cuando el servicio remoto devuelve una respuesta 4xx o 5xx, el método
generado lanza una excepción tipada en vez de devolver la carga útil de
error como si fuera un éxito. La jerarquía de excepciones vive en
`pyfly.client.exceptions`: importa de ahí las clases que quieras capturar
(por ejemplo,
`from pyfly.client.exceptions import ServiceNotFoundException`):

| Estado | Clase de excepción | `retryable` |
|---|---|---|
| 400 | `ServiceValidationException` | False |
| 401 / 403 | `ServiceAuthenticationException` | False |
| 404 | `ServiceNotFoundException` | False |
| 409 | `ServiceConflictException` | False |
| 422 | `ServiceUnprocessableEntityException` | False |
| 429 | `ServiceRateLimitException` | True |
| 5xx | `ServiceUnavailableException` | True |

Todas las excepciones extienden `ServiceClientException` (que a su vez es
una `InfrastructureException`). La marca `retryable` en
`ServiceRateLimitException` y `ServiceUnavailableException` le indica al
post-procesador qué excepciones pasar a la política de reintentos. Los
errores de validación 4xx y los 404 nunca se reintentan.

!!! note "Qué acaba de pasar"
    Las tres piezas de resiliencia encajan como cajas anidadas. Las
    **excepciones tipadas** clasifican *qué tipo* de fallo ocurrió: un 404
    no es reintentable, un 503 sí. La **política de reintentos** usa esa
    clasificación para decidir si volver a intentarlo. El **cortacircuitos**
    se sitúa fuera del bucle de reintentos y cuenta los fallos sostenidos
    para poder dejar de llamar a un servicio que está genuinamente caído.
    No escribiste nada de este pegamento: `@service_client` lo ensambló en
    el momento en que pusiste `circuit_breaker=True` y `retry=3`.

### Configurar valores por defecto en pyfly.yaml

Las anulaciones por servicio en `@service_client` siempre tienen
prioridad. Establecer valores por defecto a nivel de proceso en
`pyfly.yaml` permite que los nuevos clientes hereden valores sensatos sin
repetirlos en cada decorador:

::: listing pyfly.yaml | Listado 11.5 — Valores por defecto de resiliencia de cliente en pyfly.yaml
pyfly:
  client:
    timeout: 10
    retry:
      max-attempts: 3
      base-delay: 1.0
    circuit-breaker:
      failure-threshold: 5
      recovery-timeout: 30
:::

| Clave | Descripción | Por defecto |
|---|---|---|
| `pyfly.client.timeout` | Tiempo de espera de la petición en segundos | `30` |
| `pyfly.client.retry.max-attempts` | Total de intentos, incluido el primero | `3` |
| `pyfly.client.retry.base-delay` | Retardo base en segundos | `1.0` |
| `pyfly.client.circuit-breaker.failure-threshold` | Fallos consecutivos para abrir | `5` |
| `pyfly.client.circuit-breaker.recovery-timeout` | Segundos antes de sondear | `30` |

`ClientAutoConfiguration` lee `pyfly.client.timeout` en el arranque y se lo
pasa a `HttpxClientAdapter`. Los submapas `retry` y `circuit-breaker` se
reenvían como `default_retry` y `default_circuit_breaker` a
`HttpClientBeanPostProcessor`. Cualquier valor establecido directamente en
`@service_client(circuit_breaker_failure_threshold=...)` anula el valor por
defecto.

!!! note "Precedencia, en lenguaje llano"
    Dos capas pueden ajustar estos mandos: los valores por defecto globales
    de `pyfly.yaml` y los argumentos del decorador por cliente. El
    decorador siempre gana. Piensa en `pyfly.yaml` como el estilo de la
    casa que cada nuevo cliente hereda, y en el decorador como el lugar para
    anular ese estilo para un servicio anterior particularmente exigente.

**Ejecútalo — confirma que se lee la configuración.** Tras añadir el bloque
a `pyfly.yaml`, vuelve a leer los valores a través del `Config` de PyFly
para asegurarte de que las claves están bien escritas (una causa habitual
de "mi tiempo de espera se está ignorando" es una errata). Ejecuta esto
desde la raíz del proyecto, donde vive `pyfly.yaml`:

```
uv run python -c "
from pyfly.core.config import Config
cfg = Config.from_sources('.')
print('timeout:', cfg.get('pyfly.client.timeout'))
print('cb:', cfg.get('pyfly.client.circuit-breaker'))
"
```

Salida esperada una vez que el Listado 11.5 está en su sitio:

```
timeout: 10
cb: {'failure-threshold': 5, 'recovery-timeout': 30}
```

Antes de añadir el bloque, `timeout` reporta `30` —el valor por defecto del
framework procedente de `pyfly-defaults.yaml`—, lo que confirma que la
anulación es lo que lo cambió. Si una clave vuelve como `None`, comprueba la
indentación en `pyfly.yaml`: el anidamiento de YAML es sensible a los
espacios en blanco, y un `circuit-breaker:` mal alineado aterriza en
silencio bajo el padre equivocado.

!!! tip "Establece tiempos de espera bajos por servicio"
    El valor por defecto `timeout: 30` es conservador. En producción, cada
    servicio debería llevar una anulación en `pyfly.yaml` ajustada a su SLA.
    Una llamada de pagos que debería completarse en 500 ms debería tener
    `timeout: 2` —no 30 s— para que una instancia lenta de Payments falle
    rápido y el cortacircuitos pueda abrirse antes de que los hilos se
    acumulen.

---

## Autenticación, descubrimiento y deduplicación

### Propagar la identidad aguas abajo

Cuando el servicio Wallet llama a Payments, a menudo necesita llevar la
identidad de quien llama —un JWT o un token de servicio interno— para que
Payments pueda imponer sus propias reglas de autorización. El parámetro
`headers` recibe un trato especial por parte del post-procesador: cuando un
método stub declara `headers: dict`, el valor se reenvía como cabeceras de
la petición HTTP, no se serializa como una cadena de consulta.

!!! note "JWT, en lenguaje llano"
    Un **JWT** (JSON Web Token) es una cadena firmada que viaja en la
    cabecera `Authorization` y demuestra quién es quien llama. Cuando Wallet
    reenvía el JWT de quien llama a Payments, Payments puede volver a
    comprobarlo y aplicar sus propias reglas: la identidad se transporta a
    través de la frontera de red en lugar de restablecerse desde cero.

Para reenviar cabeceras, añades un único parámetro opcional. No hay ningún
decorador nuevo ni configuración especial:

- **Añade `headers: dict | None = None` al método.** El nombre `headers` es
  la palabra mágica: el post-procesador lo reconoce y enruta su valor a las
  cabeceras HTTP en lugar de a la cadena de consulta.
- **Pasa un dict en el punto de llamada.** El manejador suministra
  `headers={"Authorization": f"Bearer {token}"}`, y PyFly lo adjunta a la
  petición saliente.

::: listing lumen/sdk/payments_client_auth.py | Listado 11.6 — Reenvío de cabeceras de autenticación por llamada
from __future__ import annotations

from pyfly.client import get, post, service_client


@service_client(
    base_url="http://payments-service:8080",
    circuit_breaker=True,
    retry=3,
)
class AuthenticatedPaymentsClient:
    """Payments client that forwards caller identity on each request."""

    @post("/payments")
    async def create_payment(
        self,
        body: dict,
        headers: dict | None = None,
    ) -> dict:
        """POST /payments — body is the JSON payload; headers forwarded."""
        ...

    @get("/payments/{payment_id}")
    async def get_payment(
        self,
        payment_id: str,
        headers: dict | None = None,
    ) -> dict:
        """GET /payments/:payment_id — headers are forwarded."""
        ...
:::

**Cómo funciona:** El post-procesador comprueba si un parámetro llamado
`headers` está presente en los argumentos enlazados y es un `dict`. Cuando
se cumplen ambas condiciones, extrae el valor del conjunto de parámetros de
consulta y lo reenvía como cabeceras de la petición HTTP. El manejador pasa
la cabecera `Authorization` entrante (o un token de servicio recién
acuñado) como `headers={"Authorization": f"Bearer {token}"}`.

`HttpxClientAdapter` también llama a `inject_headers(headers)` en cada
petición, propagando las cabeceras W3C `traceparent` y `tracestate` del
contexto de observabilidad actual, de modo que las trazas distribuidas se
cosen a través de las fronteras de servicio sin ningún trabajo a nivel de
aplicación.

!!! note "Patrones de identidad servicio a servicio"
    Para servicios internos en una red de confianza, un secreto compartido
    en una cabecera `X-Internal-Token` es el enfoque más sencillo. Para
    arquitecturas de confianza cero (zero-trust), considera mTLS (TLS mutuo
    en la capa de infraestructura) o una malla de servicios (service mesh)
    que inyecte certificados de identidad. Para llamadas delegadas por el
    usuario, reenvía el JWT original. Sea cual sea el patrón que elijas, el
    parámetro `headers` te da un punto de inyección limpio en el cliente
    declarativo.

### Descubrimiento de servicios

Cuando `base_url` es una cadena estática como
`http://payments-service:8080`, dependes del descubrimiento basado en DNS:
un `Service` de Kubernetes o un registro de Consul resuelve
`payments-service` a la IP de clúster correcta. Este es el punto de partida
recomendado y suficiente para la mayoría de los despliegues.

Para entornos que necesitan resolución dinámica de URL (múltiples entornos
detrás de la misma clase cliente, enrutamiento controlado por feature
flags), suministra la URL a través de la configuración en su lugar:

::: listing pyfly.yaml | Listado 11.7 — URL base por entorno en pyfly.yaml
pyfly:
  client:
    timeout: 10

services:
  payments:
    base-url: "${PAYMENTS_SERVICE_URL:http://payments-service:8080}"
:::

Un bean factoría fino lee la clave de configuración y construye el
post-procesador con una factoría personalizada que inyecta la URL resuelta.
La clase cliente en sí no cambia; solo cambia la factoría.

### Deduplicación de peticiones

Las operaciones financieras deben ser idempotentes en la capa HTTP. Si
`create_payment` se llama, agota su tiempo de espera y se reintenta,
Payments no debe crear dos registros de pago. El mecanismo estándar es una
cabecera **`Idempotency-Key`**: un identificador estable elegido por quien
llama —típicamente el UUID del comando— que Payments usa para detectar y
deduplicar peticiones repetidas.

::: listing lumen/core/services/wallets/settle_transfer_idempotent.py | Listado 11.8 — Idempotency-Key reenviada a través del parámetro headers
from __future__ import annotations

from lumen.core.services.wallets.settle_transfer_command import (
    SettleTransfer,
)
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from lumen.sdk.payments_client_auth import AuthenticatedPaymentsClient
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound


@command_handler
@service
class SettleTransferIdempotentHandler(
    CommandHandler[SettleTransfer, dict]
):
    """Withdraw funds and submit payment with idempotency key."""

    def __init__(
        self,
        repository: WalletRepository,
        payments: AuthenticatedPaymentsClient,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._payments = payments

    async def do_handle(self, command: SettleTransfer) -> dict:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.withdraw(
            Money(amount=command.amount, currency=wallet.currency)
        )
        await self._repository.add(wallet)

        idempotency_key = str(command.transfer_id)
        return await self._payments.create_payment(
            body={
                "wallet_id": command.wallet_id,
                "amount": command.amount,
                "currency": wallet.currency.value,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
:::

**Cómo funciona:** `command.transfer_id` es el identificador estable de
esta operación de negocio, determinado antes de que el comando llegue al
manejador. Si el manejador se llama de nuevo para el mismo comando —desde un
reintento, una reentrega o una reproducción de la cola de mensajes muertos
(dead-letter)— pasa la misma `Idempotency-Key`. Payments almacena la clave
junto al registro de pago creado y devuelve el registro existente cuando la
clave ya se ha visto antes, en lugar de crear un segundo pago. Esa
deduplicación es una preocupación del lado del servidor; el trabajo del
cliente es simplemente reenviar la clave de forma coherente.

---

## El nivel de experiencia: el BFF

### Por qué el frontend no puede hablar directamente con ambos servicios

Cuando una aplicación móvil o un frontend web necesita mostrar un resumen
del monedero que incluya instrucciones de pago pendientes, se enfrenta a
una disyuntiva: llamar a Wallet para el saldo, llamar a Payments para la
lista de pendientes y fusionar los resultados en el cliente, o hablar con
una única API que haga la fusión en el lado del servidor. La primera opción
incurre en dos viajes de ida y vuelta, expone la forma interna de cada
servicio al cliente y obliga al cliente a implementar reintentos y manejo de
errores para dos dominios de fallo independientes. La segunda opción es el
**patrón BFF**.

Un **Backend for Frontend** es un servicio ligero en el *nivel de
experiencia* que compone respuestas de múltiples servicios de dominio en una
forma adaptada a las necesidades de un frontend concreto. Se encarga de la
agregación de respuestas, del renombrado de campos para que coincidan con
las convenciones del cliente y del cacheo de los resultados compuestos.
Nunca toca la base de datos directamente: depende por completo de clientes
de servicios de dominio.

### Construir el BFF de Lumen

El SDK de Lumen ya incluye un `LumenClient` en `lumen/sdk/client.py` que
envuelve un `httpx.AsyncClient` crudo: recibe un `httpx.AsyncClient` en su
constructor y llama a `self._http.get(...)`/`self._http.post(...)` a mano,
lanzando en caso de error con `response.raise_for_status()`. Ese es el
estilo *imperativo*: útil, explícito y enteramente responsabilidad tuya
mantenerlo resiliente. En el nivel BFF usas en su lugar el
`@service_client` declarativo de PyFly: el código que llama tiene el mismo
aspecto, pero el cortacircuitos y los reintentos vienen incorporados
automáticamente.

Construiremos el BFF en cuatro piezas pequeñas, cada una en su propio
archivo:

**Paso 1 — Un `WalletClient`** que sepa cómo alcanzar el servicio Wallet.
**Paso 2 — Un `PaymentsClient`** con un endpoint extra que el BFF necesita.
**Paso 3 — Un `WalletSummaryService`** que llame a ambos y fusione los
resultados. **Paso 4 — Un controlador fino** que exponga la vista
fusionada.

Empieza con el cliente del lado del monedero.

::: listing lumen_bff/sdk/wallet_client.py | Listado 11.9 — WalletClient para el nivel BFF
from __future__ import annotations

from pyfly.client import get, service_client


@service_client(
    base_url="http://wallet-service:8080",
    circuit_breaker=True,
    retry=3,
)
class WalletClient:
    """Typed HTTP client for the Lumen Wallet service."""

    @get("/api/v1/wallets/{wallet_id}")
    async def get_wallet(self, wallet_id: str) -> dict:
        """GET /api/v1/wallets/:wallet_id — fetch a wallet."""
        ...

    @get("/api/v1/wallets/{wallet_id}/balance")
    async def get_balance(self, wallet_id: str) -> dict:
        """GET /api/v1/wallets/:wallet_id/balance — current balance."""
        ...
:::

Las rutas reflejan el controlador real de Lumen —`@request_mapping("/api/v1/wallets")` con `@get_mapping("/{wallet_id}")`— así que el cliente del BFF coincide exactamente con lo que el servicio Wallet expone.

El servicio Payments necesita un endpoint `list_pending` que permita al BFF
consultar registros pendientes por monedero:

::: listing lumen_bff/sdk/payments_client_bff.py | Listado 11.10 — PaymentsClient ampliado para el BFF
from __future__ import annotations

from pyfly.client import get, post, service_client


@service_client(
    base_url="http://payments-service:8080",
    circuit_breaker=True,
    retry=3,
)
class PaymentsClient:
    """Typed HTTP client for Payments (BFF edition)."""

    @post("/payments")
    async def create_payment(self, body: dict) -> dict:
        """POST /payments — submit a payment instruction."""
        ...

    @get("/payments/{payment_id}")
    async def get_payment(self, payment_id: str) -> dict:
        """GET /payments/:payment_id — fetch a payment by ID."""
        ...

    @get("/payments")
    async def list_pending(self, wallet_id: str) -> list:
        """GET /payments?wallet_id=... — list payments for a wallet."""
        ...
:::

El servicio BFF compone entonces el saldo del monedero con la lista de
pagos pendientes en una única respuesta.

!!! note "asyncio.gather, en lenguaje llano"
    `asyncio.gather(a, b)` arranca las corrutinas `a` y `b` al mismo tiempo
    y espera a que ambas terminen, devolviendo sus resultados como una
    lista. Para un BFF esto significa que dos llamadas aguas arriba se
    solapan en vez de encolarse una tras otra. Añadir
    `return_exceptions=True` cambia una cosa: en lugar de que todo el
    `gather` reviente cuando una llamada falla, la excepción de la llamada
    fallida se devuelve como el *resultado* de esa llamada, de modo que
    puedes inspeccionarla y aun así usar la exitosa.

::: listing lumen_bff/application/bff_service.py | Listado 11.11 — Servicio BFF que compone Wallet + Payments
from __future__ import annotations

import asyncio

from lumen_bff.sdk.payments_client_bff import PaymentsClient
from lumen_bff.sdk.wallet_client import WalletClient
from pyfly.container import service


@service
class WalletSummaryService:
    """Composes wallet balance and pending payments into one view.

    Calls both domain services concurrently using asyncio.gather so the
    total latency is max(wallet_latency, payments_latency) rather than
    their sum.
    """

    def __init__(
        self,
        wallet: WalletClient,
        payments: PaymentsClient,
    ) -> None:
        self._wallet = wallet
        self._payments = payments

    async def get_summary(self, wallet_id: str) -> dict:
        """Return a unified summary for the given wallet."""
        wallet_data, pending = await asyncio.gather(
            self._wallet.get_wallet(wallet_id),
            self._payments.list_pending(wallet_id),
            return_exceptions=True,
        )

        balance_minor: int = 0
        if isinstance(wallet_data, dict):
            balance_minor = wallet_data.get("balance_minor", 0)

        pending_list: list = []
        if isinstance(pending, list):
            pending_list = pending

        return {
            "wallet_id": wallet_id,
            "balance_minor": balance_minor,
            "pending_payments": pending_list,
        }
:::

**Cómo funciona — el patrón de composición:**

`asyncio.gather(...)` dispara ambas llamadas aguas arriba de forma
concurrente. Las llamadas a wallet y a payments se ejecutan en paralelo,
así que la latencia compuesta está acotada por la más lenta de las dos en
vez de por su suma: a 50 ms por servicio, las llamadas secuenciales cuestan
100 ms mientras que las concurrentes cuestan aproximadamente 55 ms.

`return_exceptions=True` es crítico para un BFF. Sin él, un único fallo
aguas arriba lanza una excepción y quien llama no recibe nada. Con él, una
corrutina fallida devuelve su objeto excepción como resultado en lugar de
propagarlo. El servicio inspecciona cada resultado con
`isinstance(wallet_data, dict)` y degrada con elegancia: devuelve una
respuesta parcial con un saldo cero o una lista de pagos vacía en vez de un
HTTP 500. El BFF debería hacer esa decisión explícita en la forma de su
respuesta, por ejemplo incluyendo una clave `"errors"` que enumere los
campos degradados.

El nombre de campo `balance_minor` sigue la convención de Lumen: las
cantidades se almacenan como unidades menores enteras (céntimos) y el campo
se llama `balance_minor` en todas partes: en `WalletDto`, en las respuestas
de depósito/reintegro y aquí en el resumen del BFF.

Cada envoltura `@service_client` sobre `WalletClient` y `PaymentsClient`
gestiona los reintentos y el cortacircuitos para su llamada aguas arriba de
forma independiente. Si Payments tiene el circuito abierto, el saldo del
monedero sigue apareciendo; solo la lista de pagos pendientes está vacía.

### El controlador del BFF

El BFF expone su respuesta compuesta a través de un manejador web estándar
de PyFly. El controlador es deliberadamente fino: su única misión es
delegar en el servicio:

::: listing lumen_bff/web/controllers/summary_controller.py | Listado 11.12 — Controlador del BFF
from __future__ import annotations

from lumen_bff.application.bff_service import WalletSummaryService
from pyfly.container import rest_controller
from pyfly.web import get_mapping, request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletSummaryController:
    """Experience-tier controller for the wallet summary view."""

    def __init__(self, summary: WalletSummaryService) -> None:
        self._summary = summary

    @get_mapping("/{wallet_id}/summary")
    async def get_wallet_summary(self, wallet_id: str) -> dict:
        """GET /api/v1/wallets/:wallet_id/summary"""
        return await self._summary.get_summary(wallet_id)
:::

**Cómo funciona:** El controlador del BFF no importa ningún modelo de
dominio y no toca ningún repositorio: depende solo de
`WalletSummaryService`, que a su vez depende solo de interfaces de cliente
tipadas. La cadena de dependencias es controlador → servicio BFF →
clientes declarativos → HTTP remoto. Cada capa es comprobable de forma
independiente: el controlador con un servicio simulado (mock), el servicio
con clientes simulados, y los clientes con un `HttpClientPort` simulado.

**Ejecútalo — llama al endpoint compuesto.** Con la aplicación BFF en
ejecución (`uv run pyfly run --server uvicorn`) y los servicios Wallet y
Payments alcanzables, llama a la ruta del resumen con un id de monedero que
ya hayas creado:

```
curl -s http://localhost:8080/api/v1/wallets/wal-123/summary
```

Forma esperada (los valores dependen de tus datos):

```
{"wallet_id": "wal-123", "balance_minor": 5000, "pending_payments": []}
```

La respuesta única fusiona dos servicios aguas arriba. Para ver la
degradación elegante en acción, detén el servicio Payments y vuelve a
llamar: el `balance_minor` sigue volviendo desde Wallet, y
`pending_payments` recurre a `[]` en lugar de que toda la petición devuelva
un 500. Eso es `return_exceptions=True` haciendo su trabajo.

!!! note "Puerto por defecto de la aplicación"
    PyFly sirve la aplicación en `pyfly.server.port`, cuyo valor por
    defecto es `8080` (coincidiendo con `server.port` de Spring). Anúlalo
    con la variable de entorno `PYFLY_SERVER_PORT` o con la clave de
    configuración `pyfly.server.port`. Ten en cuenta que el actuator y el
    panel de administración se ejecutan en un puerto de gestión *separado*
    —`pyfly.management.server.port`, por defecto `9090`— de modo que los
    endpoints de salud e información nunca chocan con tus rutas de API.

!!! note "Alcance del BFF y propiedad del equipo"
    Un BFF tiene como alcance un frontend o un recorrido de usuario, no uno
    por microservicio. Lumen podría tener un `lumen-mobile-bff` y un
    `lumen-web-bff`, cada uno componiendo los mismos servicios de dominio
    pero devolviendo formas optimizadas para sus respectivos clientes. El
    BFF es propiedad del equipo de frontend, no del equipo de dominio. Los
    servicios de dominio exponen contratos estables; los BFF adaptan esos
    contratos a formas específicas del cliente sin acoplar los servicios de
    dominio a las convenciones de ningún frontend en particular.

!!! spring "Equivalencia con Spring"
    El patrón BFF en PyFly refleja el enfoque API Gateway / BFF de Spring
    Boot, donde una aplicación fina de Spring Boot agrega respuestas de
    múltiples microservicios. En la pila reactiva de Spring, `Mono.zip()`
    proporciona la misma agregación concurrente que `asyncio.gather()`
    proporciona en Python. El `@FeignClient` en el BFF se corresponde con
    `@service_client` en PyFly; el enfoque del `WebClient` de Spring de
    encadenar llamadas `.flatMap()` se corresponde con la combinación de
    `asyncio.gather()` + manejo de errores con `isinstance` de PyFly. El
    modelo de propiedad por equipos —el BFF es propiedad del equipo de
    frontend, los servicios de dominio son propiedad de los equipos de
    dominio— es idéntico.

---

## Lo que construiste {.recap}

Empezaste este capítulo con un único servicio Lumen y lo terminaste con una
arquitectura que escala en múltiples dimensiones. Esto es lo que cambió y
por qué importa.

**Extrajiste PaymentsService.** Payments ahora se ejecuta en su propio
proceso, con su propia canalización de despliegue y su propio almacén de
datos. Los manejadores de Wallet no saben nada de cómo Payments almacena
los registros de pago ni qué motor de base de datos usa: todo lo que ven es
la interfaz tipada que `PaymentsClient` expone.

**Declaraste el cliente, no la implementación.** `@service_client` con
stubs `@post`, `@get`, `@patch` y `@delete` te dio una interfaz tipada que
los IDE navegan y los verificadores de tipos comprueban.
`HttpClientBeanPostProcessor` generó la implementación HTTP en el arranque,
leyó la configuración de resiliencia de `pyfly.yaml` y registró el bean para
inyectarlo en cualquier sitio.

**Hiciste la red resiliente.** `circuit_breaker=True` y `retry=3` en el
decorador envolvieron cada método con un cortacircuitos y una política de
reintentos compartidos —cortacircuitos fuera, reintento dentro— de modo que
una caída sostenida de Payments abre el circuito rápido mientras que los
errores transitorios se recuperan automáticamente. Las excepciones tipadas
(`ServiceNotFoundException`, `ServiceUnavailableException`) dan a quien
llama una señal limpia sin exponer códigos de estado HTTP en crudo.

**Introdujiste el nivel BFF.** `WalletSummaryService` compone dos llamadas
aguas arriba con `asyncio.gather`, devuelve una respuesta parcial cuando uno
de los servicios está degradado y expone un único contrato al frontend. El
BFF absorbe el ciclo de publicación independiente de cada servicio de
dominio y protege al frontend de sus formas internas.

Tres principios atraviesan el resto de la Parte IV:

- **Depende del cliente tipado, no de `httpx` directamente.** La
  declaración es tu contrato; la implementación es un detalle del framework.
- **La resiliencia pertenece a la capa de cliente.** Configúrala una vez en
  `@service_client`; cada manejador que use el cliente la hereda.
- **Los BFF componen; los servicios de dominio proveen.** Los servicios de
  dominio poseen contratos estables y de grano fino; los BFF poseen las
  composiciones de grano grueso que necesitan frontends concretos.

---

## Pruébalo tú mismo {.exercises}

1. **Añade un cuarto endpoint y verifica la interpolación de rutas.** Amplía
   `PaymentsClient` con un método `@get("/payments")` que acepte
   `wallet_id: str` y `status: str = "pending"` como parámetros. Llámalo
   desde una prueba y afirma que la petición HTTP generada es
   `GET /payments?wallet_id=abc&status=pending`. Verifica que cambiar el
   valor por defecto a `status="completed"` y llamar al método sin un
   argumento `status` envía `status=completed` en la cadena de consulta.

2. **Prueba el BFF con servicios aguas arriba degradados.** Escribe una
   prueba unitaria para `WalletSummaryService.get_summary` que simule
   `WalletClient.get_wallet` para que tenga éxito y
   `PaymentsClient.list_pending` para que lance `ServiceUnavailableException`
   (impórtala con
   `from pyfly.client.exceptions import ServiceUnavailableException`).
   Afirma que el método devuelve un dict con el `balance_minor` correcto y
   una lista `pending_payments` vacía, confirmando que el repliegue a
   respuesta parcial funciona y que un único fallo aguas arriba no se
   propaga como excepción hacia quien llama al BFF. Ejecútala con
   `uv run --extra dev pytest tests/test_wallet_summary.py -q` y busca
   `1 passed`.

3. **Ajusta los umbrales del cortacircuitos para un servicio aguas arriba
   frágil.** Supón que Payments tiene una ventana conocida de
   inestabilidad durante su ejecución nocturna por lotes: devuelve 503 en
   aproximadamente el 20 % de las peticiones durante unos 10 segundos antes
   de estabilizarse. Configura `PaymentsClient` con
   `circuit_breaker_failure_threshold=2` y
   `circuit_breaker_recovery_timeout=15.0` y escribe una prueba usando un
   `HttpClientPort` simulado que simule dos fallos consecutivos seguidos de
   un éxito. Afirma que la tercera llamada (el sondeo tras el tiempo de
   espera de recuperación) tiene éxito y que el circuito transita de vuelta
   a `CLOSED`.
