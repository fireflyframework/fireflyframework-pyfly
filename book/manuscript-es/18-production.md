<span class="eyebrow">Capítulo 18</span>

# Extender PyFly y llevarlo a producción {.chtitle}

::: figure art/openers/ch18.svg | &nbsp;

Lumen ya no es un juguete. A lo largo de los diecisiete capítulos anteriores construiste un servicio de monedero (wallet) desde una única clase anotada hasta un microservicio completo orientado a eventos: CQRS, sagas, EDA, clientes HTTP, caché, resiliencia, observabilidad, seguridad y un conjunto de pruebas respaldado por contenedores reales. Sabes cómo ejecutarlo, depurarlo y desplegarlo.

Este capítulo final trata sobre la distancia que separa el "funciona en mi portátil" del "funciona de forma fiable para usuarios reales". Esa distancia tiene tres dimensiones. Primera: la **extensibilidad**, la capacidad de añadir comportamiento sin bifurcar el framework. Segunda: las funcionalidades que tu dominio puede necesitar ahora mismo: un motor de reglas, configuración centralizada, varios idiomas, push en tiempo real, una CLI. Tercera: los hábitos operativos que separan un proyecto de fin de semana de un servicio en producción.

Avanzarás rápido. Cada sección escoge un tema, muestra la API mínima que funciona y la conecta con Lumen. Al final tendrás una imagen completa del ecosistema PyFly y una lista de comprobación de producción que merece la pena tener a mano.

!!! note "Convenciones de este capítulo"
    Los listados y las claves de configuración de aquí apuntan a PyFly
    **v26.6.110**. Dos hechos de despliegue de esa versión recorren toda la
    sección "Llevarlo a producción", así que conviene conocerlos de
    antemano:

    - La aplicación escucha en `pyfly.server.port` (por defecto `8080`), la
      clave equivalente a `server.port` de Spring. Las antiguas claves
      `pyfly.web.port` / `PYFLY_WEB_PORT` se eliminaron en v26.6.102.
    - El Actuator y el panel de administración se ejecutan en un **puerto de
      gestión independiente** (`pyfly.management.server.port`, por defecto
      `9090`), abierto y sin autenticación por defecto. Lo cubrimos en detalle
      cuando conectemos las sondas de salud.

    Cada funcionalidad de abajo se construye igual: un breve "por qué", el
    código y, a continuación, un punto de control **Ejecútalo** que muestra el
    comando exacto y la salida que deberías ver. Escribe los comandos a medida
    que avanzas: así es como las ideas se asientan.

---

## Plugins y puntos de extensión

### ¿Por qué un sistema de plugins?

El framework central es pequeño de forma intencionada. Las funcionalidades opcionales (formateadores, sumideros de auditoría, canales de notificación) deberían poder conectarse como plugins para que los equipos compongan solo lo que necesitan y publiquen añadidos sin tocar las interioridades del framework.

El módulo `pyfly.plugins` de PyFly refleja el registro de plugins de Spring: declaras un **punto de extensión** (una ranura con nombre), **extensiones** (contribuciones concretas) y las agrupas en un **plugin** con un ciclo de vida.

La nueva jerga, en términos sencillos: un **punto de extensión** es un hueco etiquetado en tu aplicación: "todo lo que quiera ser un sumidero de auditoría se conecta aquí". Una **extensión** es una cosa que llena el hueco. Un **plugin** es el paquete que entrega una o más extensiones juntas y que se puede arrancar y detener como una unidad. Si alguna vez has definido una interfaz de Java y has dejado que quienes la llaman registren implementaciones de ella, ya conoces la forma: los decoradores de abajo simplemente hacen el cableado declarativo.

Construiremos el ejemplo útil más pequeño: un sumidero de auditoría de consola que imprime cada evento que se le entrega.

**Paso 1: declara el punto de extensión.** Este es el contrato. Todo sumidero de auditoría debe prometer un método `record(event)`. La cadena `id="audit-sinks"` es el nombre que usa el resto del código para encontrar más tarde cada sumidero.

**Paso 2: escribe un plugin que aporte un sumidero.** El decorador `@plugin` envuelve una clase con un `id` y una `version` obligatorios; la clase interna `@extension` es la contribución real. Hereda de la interfaz del punto de extensión (`AuditSink`) para que el registro pueda confirmar que respeta el contrato.

**Paso 3: dale al plugin un ciclo de vida.** Los métodos `start` y `stop` son donde abres y cierras recursos (un descriptor de archivo, una conexión de red). El gestor de plugins los llama por ti.

Aquí tienes los tres pasos en un único archivo.

::: listing lumen/plugins/audit.py | Listado 18.1 — Declarar un plugin de sumidero de auditoría
from pyfly.plugins import (
    PluginManager,
    extension,
    extension_point,
    plugin,
)


@extension_point(id="audit-sinks")
class AuditSink:
    """Interface that all audit sinks must implement."""

    def record(self, event: dict) -> None: ...


@plugin(id="console-audit", version="1.0.0")
class ConsoleAuditPlugin:

    @extension(point="audit-sinks", priority=10)
    class ConsoleSink(AuditSink):
        name = "console"

        def record(self, event: dict) -> None:
            print(f"[AUDIT] {event}")

    async def start(self) -> None:
        print("ConsoleAuditPlugin started")

    async def stop(self) -> None:
        print("ConsoleAuditPlugin stopped")
:::

**Cómo funciona.** `@extension_point(id="audit-sinks")` registra una ranura con nombre y declara la interfaz que toda contribución debe implementar. `@plugin(id="console-audit", version="1.0.0")` declara una clase de plugin con un `id` y una `version` obligatorios. `@extension(point="audit-sinks", priority=10)` marca una clase interna como contribución a esa ranura; la clase interna debe heredar de la interfaz del punto de extensión para que el registro pueda validarla. La prioridad más alta gana la primera posición al iterar los resultados.

Cargar y ejecutar el plugin:

::: listing lumen/plugins/runner.py | Listado 18.2 — Conducir el ciclo de vida del plugin
import asyncio

from pyfly.plugins import PluginManager

from lumen.plugins.audit import ConsoleAuditPlugin


async def main() -> None:
    manager = PluginManager()
    await manager.add(ConsoleAuditPlugin)
    await manager.start_all()

    sinks = await manager.registry.get("audit-sinks")
    for sink in sinks:
        sink.record({"action": "deposit", "amount_minor": 100})

    await manager.stop_all()


asyncio.run(main())
:::

`PluginManager.add()` inspecciona la clase en busca de declaraciones `@extension_point` anidadas y luego registra cada contribución `@extension`. `start_all()` invoca los hooks `init` y después `start` de cada plugin en orden de dependencias; `stop_all()` invierte la secuencia, llamando a `stop` y luego a `unload`. Las dependencias circulares lanzan `PluginResolutionError` antes de que se ejecute código alguno.

!!! tip "Ejecútalo"
    Guarda ambos listados bajo `src/lumen/plugins/` y ejecuta el módulo runner
    directamente:

    ```bash
    uv run python -m lumen.plugins.runner
    ```

    Deberías ver dispararse los hooks del ciclo de vida, registrarse el único
    evento y producirse el apagado limpio:

    ```
    ConsoleAuditPlugin started
    [AUDIT] {'action': 'deposit', 'amount_minor': 100}
    ConsoleAuditPlugin stopped
    ```

    El orden lo es todo: `start` se ejecuta antes de usar ningún sumidero, tu
    código itera los sumideros registrados y `stop` se ejecuta el último. Si
    añades un segundo plugin más adelante, `start_all()` arranca ambos en orden
    de dependencias y `stop_all()` los apaga en orden inverso: nunca gestionas
    ese orden a mano.

**Lo que acaba de ocurrir.** Añadiste una capacidad a la aplicación —el
registro de auditoría— sin editar una sola línea de código del framework. El
framework descubrió tu plugin, validó que su extensión respeta el contrato
`AuditSink`, ejecutó su ciclo de vida y te entregó los sumideros registrados
para que los llamaras. Esa es la promesa central de un sistema de plugins:
abierto a la extensión, cerrado a la modificación.

| Método | Descripción |
|---|---|
| `await manager.add(cls)` | Escanea y registra una clase de plugin |
| `await manager.start_all()` | `init` → `start` en orden de dependencias |
| `await manager.stop_all()` | `stop` → `unload` en orden inverso |
| `await manager.remove(plugin_id)` | Descarga un plugin; devuelve `False` si es desconocido |
| `await manager.registry.get(point_id)` | Extensiones de una ranura, ordenadas por prioridad |

!!! spring "Equivalencia con Spring"
    `@plugin` / `@extension_point` / `@extension` reflejan
    `@Plugin` / `ExtensionPoint` / `@Extension` de la API de plugins
    de Spring. `PluginManager.start_all()` desempeña el papel de la
    gestión del ciclo de vida del contenedor de plugins de Spring. El
    arranque en orden de dependencias y el apagado en orden inverso son
    idénticos en semántica.

---

## Reglas de negocio con el motor de reglas

La mayoría de los servicios del mundo real arrastran lógica que pertenece al negocio, no al código: "marca los pedidos de más de 500.000 céntimos", "bloquea los envíos a regiones sancionadas", "aplica un recargo fuera de horario". Codificar esos umbrales en Python a fuego significa recompilar cada vez que el negocio cambia de opinión.

El módulo `pyfly.rule_engine` de PyFly ofrece a los responsables de producto un dial YAML que pueden girar sin tocar el código fuente.

Un **motor de reglas**, en términos sencillos, es un pequeño intérprete de sentencias "si esto, entonces aquello" que viven en datos en lugar de en código. Le proporcionas un saco de hechos (el *contexto*) y un conjunto de reglas; comprueba la condición de cada regla contra los hechos y, para las que coinciden, ejecuta las acciones listadas, normalmente escribiendo una marca de vuelta en el contexto. La ventaja es que las *reglas* las pueden editar quienes no programan, mientras que el *motor* que las ejecuta es fijo y está probado.

### Definir reglas en YAML

Construiremos una comprobación de fraude y límite en dos pasos: primero escribir las reglas como datos, luego conectar un servicio que las ejecuta.

**Paso 1: escribe las reglas como YAML.** Cada regla nombra una condición `when` y una lista de acciones `then`. Como las reglas son datos planos, un responsable de producto puede revisarlas en una pull request y un servidor de configuración puede intercambiarlas en caliente en tiempo de ejecución.

Las reglas viven en un archivo YAML separado que cualquier miembro del equipo puede revisar. El evaluador analiza este archivo una vez al arranque, o en cada obtención si lo recargas en caliente desde un Config Server (consulta la siguiente sección).

::: listing lumen/rules/transaction_rules.yaml | Listado 18.3 — Reglas de fraude y límite diario (importes en unidades menores)
id: transaction-rules
name: Lumen transaction rules

rules:
  - id: daily-limit
    priority: 20
    when:
      op: ge
      field: transaction.daily_total
      value: 500000
    then:
      - type: set
        target: flags.limit_exceeded
        value: true
      - type: log
        value: "daily limit exceeded"

  - id: fraud-country
    priority: 10
    when:
      op: in
      field: transaction.country
      value: ["XX", "YY", "ZZ"]
    then:
      - type: set
        target: flags.fraud_risk
        value: true
      - type: log
        value: "high-risk country detected"

  - id: high-value
    priority: 5
    when:
      op: ge
      field: transaction.amount
      value: 100000
    then:
      - type: set
        target: flags.high_value
        value: true
:::

Cada regla tiene una condición `when` y una lista de acciones `then`. Los importes son siempre **unidades menores enteras** (céntimos), así que `100000` son 1.000,00 €. Las condiciones usan estos operadores:

| Comparación | Lógicos |
|---|---|
| `eq`, `ne`, `gt`, `ge`, `lt`, `le` | `and`, `or`, `not` |
| `in`, `not_in`, `regex` | (con `conditions: [...]`) |

Las acciones son `set` (escribir en una ruta del contexto), `increment` o `log`. Crea una subclase de `RuleEvaluator` y sobreescribe `_execute_action` para añadir `call`, `calculate` o cualquier verbo personalizado.

**Paso 2: conecta un servicio que cargue y ejecute las reglas.** El servicio analiza el YAML una vez en la construcción y luego expone un método `assess()` que construye un contexto, ejecuta el evaluador y devuelve las marcas que las reglas establecieron.

::: listing lumen/rules/risk_service.py | Listado 18.4 — Evaluar reglas contra una transacción
from pathlib import Path

from pyfly.container import service
from pyfly.rule_engine import RuleSetEvaluator, RuleSetLoader


@service
class RiskService:
    """Evaluate transaction-level rules and return risk flags."""

    def __init__(self) -> None:
        yaml_text = (
            Path(__file__).parent / "transaction_rules.yaml"
        ).read_text()
        self._ruleset = RuleSetLoader.from_yaml(yaml_text)
        self._evaluator = RuleSetEvaluator()

    def assess(
        self,
        amount: int,
        daily_total: int,
        country: str,
    ) -> dict:
        ctx = {
            "transaction": {
                "amount": amount,
                "daily_total": daily_total,
                "country": country,
            },
            "flags": {},
        }
        self._evaluator.evaluate(self._ruleset, ctx)
        return ctx["flags"]
:::

**Cómo funciona.** `RuleSetLoader.from_yaml(text)` analiza el YAML en un AST. `RuleSetEvaluator.evaluate(ruleset, ctx)` recorre cada regla en orden de prioridad, evalúa la cláusula `when` y aplica las acciones `then` que coinciden, mutando `ctx` en el sitio y devolviendo una `list[EvaluationResult]`. El diccionario `flags` es la salida autoritativa: un manejador (handler) posterior rechaza, encola o marca la transacción en función de las claves que se hayan establecido. `amount` y `daily_total` están en unidades menores (céntimos) para coincidir con el dominio de Lumen.

!!! tip "Ejecútalo"
    Ejercita el servicio desde un REPL de Python para ver dispararse las
    reglas. Una transferencia de 6.000,00 € (`600000` unidades menores)
    dispara a la vez los umbrales de alto valor y de límite diario:

    ```bash
    uv run python -c "
    from lumen.rules.risk_service import RiskService
    print(RiskService().assess(amount=600000, daily_total=600000, country='US'))
    "
    ```

    Salida esperada: las marcas que establecen las reglas coincidentes, y nada
    más:

    ```python
    {'high_value': True, 'limit_exceeded': True}
    ```

    Ahora baja el importe a `50000` (500,00 €) con un país limpio y recibes de
    vuelta un `{}` vacío: ninguna regla coincidió, así que no se marcó nada.
    Cambiaste el *resultado* sin tocar Python: ese es el dial que el YAML da a
    tus responsables de producto.

::: figure art/figures/18-production.svg | Figura 18.1 — Evaluación de reglas en la frontera del servicio. Las reglas YAML se analizan una vez al arranque; cada transacción pasa por el evaluador como un diccionario de contexto mutable.

!!! tip "Recargar reglas en caliente sin redesplegar"
    Almacena `transaction_rules.yaml` en el Config Server (consulta la
    siguiente sección) y vuelve a analizarlo en cada obtención. Tu motor de
    reglas se convierte en un dial en vivo que controla el negocio.

---

## Configuración centralizada (Config Server)

A medida que Lumen crece a múltiples servicios, cada uno arrastra su propia copia de URLs de base de datos, tiempos de espera y feature flags. El módulo Config Server elimina esa duplicación: un servicio posee la verdad; todos los demás la obtienen al arrancar.

Un **config server**, en términos sencillos, es un pequeño servicio HTTP cuyo único trabajo es repartir configuración. En lugar de incrustar tiempos de espera y URLs en el `pyfly.yaml` de cada servicio, los almacenas en un único lugar y cada servicio pide su paquete al arrancar. Cambia el valor una vez, reinicia (o refresca) a los consumidores y toda la flota se mueve junta.

### Ejecutar el servidor

Habilita el servidor en `pyfly.yaml`:

```yaml
pyfly:
  config-server:
    enabled: true
    backend:
      root: /etc/lumen/config
```

Eso es todo. PyFly autoconfigura un `ConfigServer` respaldado por un `FilesystemConfigBackend` y monta las rutas HTTP automáticamente:

| Método | Ruta | Propósito |
|---|---|---|
| `GET` | `/{app}/{profile}` | Obtener el paquete de configuración combinado |
| `GET` | `/{app}/{profile}/{label}` | Obtener para una etiqueta concreta |
| `POST` | `/{app}/{profile}` | Guardar un paquete de configuración |
| `GET` | `/_list` | Listar todos los paquetes almacenados |

La forma de la respuesta es compatible con Spring Cloud Config, así que un servicio Spring existente puede consumir el mismo endpoint sin cambios.

### Guardar y obtener configuración de forma programática

::: listing lumen/config/seed.py | Listado 18.5 — Sembrar y leer un paquete de configuración
import asyncio

from pyfly.config_server import (
    ConfigClient,
    ConfigServer,
    FilesystemConfigBackend,
)


async def seed() -> None:
    server = ConfigServer(FilesystemConfigBackend("/etc/lumen/config"))
    await server.save(
        "wallet",
        "prod",
        {
            "db.url": "postgres://db:5432/lumen",
            "cache.ttl": 30,
        },
    )
    bundle = await server.fetch("wallet", "prod")
    print(bundle)


asyncio.run(seed())
:::

!!! tip "Ejecútalo"
    Con `pyfly.config-server.enabled: true` en `pyfly.yaml`, arranca la app y
    haz un curl al paquete que sembraste. Recuerda: las rutas del config-server
    se sirven en el puerto de **aplicación** (`8080`), no en el puerto de
    gestión.

    ```bash
    uv run pyfly run
    # en otra terminal:
    curl http://localhost:8080/wallet/prod
    ```

    La respuesta tiene la forma de Spring Cloud Config: tus claves sembradas
    llegan dentro de un array `propertySources`:

    ```json
    {
      "name": "wallet",
      "profiles": ["prod"],
      "propertySources": [
        {"name": "wallet-prod", "source": {"db.url": "postgres://db:5432/lumen", "cache.ttl": 30}}
      ]
    }
    ```

    Como la forma coincide con Spring Cloud Config, un servicio Spring Boot
    existente puede apuntar su `spring.config.import=configserver:` a esta misma
    URL y consumirla sin cambios.

Los servicios cliente la obtienen al arrancar con:

::: listing lumen/config/bootstrap.py | Listado 18.6 — Obtener configuración remota al arranque
from pyfly.config_server import ConfigClient


async def load_remote() -> dict:
    client = ConfigClient(
        url="http://config:8888",
        application="wallet",
        profile="prod",
        label="main",
    )
    return await client.fetch()
:::

`ConfigClient.fetch()` hace un GET a `{url}/{application}/{profile}/{label}`, combina el array `propertySources` (la prioridad más alta primero) y devuelve un diccionario plano `{dotted_key: value}`. En el funcionamiento normal nunca llamas a `ConfigClient` directamente: establece `pyfly.cloud.config.uri` en `pyfly.yaml` y `PyFlyApplication` lo llama automáticamente durante el bootstrap, combinando el resultado en el `Config` de la aplicación como una fuente de alta precedencia.

!!! note "Prioridad de respaldo"
    El servidor ensambla hasta cuatro capas de superposición:
    `{app}/{profile}`, `{app}/default`, `application/{profile}`,
    `application/default`. Un cliente las combina con la primera fuente
    ganando, así que las anulaciones específicas de entorno siempre baten a
    los valores por defecto compartidos.

---

## Internacionalización (i18n)

Los mensajes de error y las notificaciones de Lumen viven actualmente como literales de cadena en Python. Cuando los usuarios hablan idiomas diferentes, ese enfoque no escala.

**i18n** es la abreviatura de *internacionalización* (las 18 letras entre la "i" y la "n"). En la práctica significa sacar cada cadena visible para el usuario de tu código hacia **paquetes de recursos** por idioma, y luego elegir el paquete correcto en tiempo de ejecución según el idioma preferido de quien llama. Tu código se refiere a una cadena mediante una clave estable como `wallet.deposit_ok`; el framework busca la traducción.

Habilita el subsistema i18n con un único flag:

```yaml
pyfly:
  i18n:
    enabled: true
    base-path: i18n/
    default-locale: en
```

### Escribir paquetes de recursos

**Paso 1: escribe un paquete por idioma.** Los archivos se nombran `messages_<locale>.yaml`. Las claves se comparten entre idiomas; solo cambian los valores. Los marcadores `{0}`, `{1}` son marcadores de posición posicionales que el framework rellena en tiempo de renderizado.

```yaml
# i18n/messages_en.yaml
wallet:
  deposit_ok: "Deposited {0} minor units to wallet {1}."
  limit_exceeded: "Daily limit exceeded. Maximum is {0} minor units."

# i18n/messages_es.yaml
wallet:
  deposit_ok: "Se depositaron {0} unidades menores en la billetera {1}."
  limit_exceeded: "Se superó el límite diario. El máximo es {0} unidades menores."
```

**`ResourceBundleMessageSource`** resuelve claves con notación de puntos y sustituye los marcadores de posición `{n}` (base cero) siguiendo la semántica de `MessageFormat`. Los códigos ausentes recurren al `default-locale`; si tampoco están allí, `get_message` lanza `KeyError`.

### Usar MessageSource en un servicio

**Paso 2: inyecta `MessageSource` y resuelve el locale a partir de la petición.** El servicio de abajo lee la cabecera `Accept-Language` de quien llama, escoge el paquete que coincide y renderiza el mensaje con los argumentos en tiempo de ejecución.

::: listing lumen/i18n/notification_service.py | Listado 18.7 — Servicio de notificación consciente del locale
from pyfly.container import service
from pyfly.i18n import AcceptHeaderLocaleResolver, MessageSource


@service
class NotificationService:
    """Renders user-facing messages in the caller's preferred locale."""

    def __init__(
        self,
        messages: MessageSource,
        locale_resolver: AcceptHeaderLocaleResolver,
    ) -> None:
        self._messages = messages
        self._resolver = locale_resolver

    def deposit_confirmation(
        self,
        request,
        amount_minor: int,
        wallet_id: str,
    ) -> str:
        locale = self._resolver.resolve_locale(request)
        return self._messages.get_message_or_default(
            "wallet.deposit_ok",
            default="Deposit successful.",
            args=(amount_minor, wallet_id),
            locale=locale,
        )
:::

`AcceptHeaderLocaleResolver` analiza la cabecera `Accept-Language` y devuelve el subtag primario con la `q` más alta. Usa `FixedLocaleResolver` para despliegues de un solo idioma o para pruebas. La autoconfiguración registra ambos cuando `pyfly.i18n.enabled: true`; inyecta o bien `MessageSource` (el protocolo del puerto) o bien el `ResourceBundleMessageSource` concreto: ambos funcionan.

!!! tip "Ejecútalo"
    La forma más rápida de demostrar que la búsqueda funciona es una prueba que
    resuelve el paquete directamente, sin necesidad de un servidor HTTP. Guarda
    el Listado 18.7a bajo `tests/` y luego ejecuta solo esta prueba:

    ```bash
    uv run --extra dev pytest tests/test_messages.py -q
    ```

    Esperado:

    ```
    1 passed in 0.05s
    ```

    Cambia `locale="es"` por `locale="en"` y la aserción necesitaría la cadena
    en inglés en su lugar: misma clave, paquete distinto. Ese es justo el
    objetivo de i18n: tu código nunca cambia, solo cambia el locale resuelto.

::: listing tests/test_messages.py | Listado 18.7a — Afirmar un mensaje traducido
from pyfly.i18n.adapters.resource_bundle import ResourceBundleMessageSource


def test_deposit_message_in_spanish() -> None:
    messages = ResourceBundleMessageSource(base_path="i18n/", default_locale="en")
    text = messages.get_message(
        "wallet.deposit_ok", args=(100, "w-001"), locale="es"
    )
    assert text == "Se depositaron 100 unidades menores en la billetera w-001."
:::

!!! spring "Equivalencia con Spring"
    `MessageSource`, `ResourceBundleMessageSource`,
    `AcceptHeaderLocaleResolver` y `FixedLocaleResolver` son
    equivalentes directos de nombre del stack i18n de Spring MVC. La API
    difiere solo en el uso de marcadores de posición posicionales `{n}` en
    lugar de SpEL dentro de las cadenas de mensaje.

---

## Actualizaciones en tiempo real con WebSocket

El panel de administración de Lumen actualmente sondea en busca de cambios de saldo. Un endpoint WebSocket elimina el sondeo: el servidor empuja una actualización en el instante en que un depósito se confirma.

Un **WebSocket** es una conexión bidireccional que permanece abierta. Una petición HTTP normal es de un solo disparo: el cliente pregunta, el servidor responde, la línea se cierra. Con un WebSocket la línea se mantiene activa, así que el *servidor* puede enviar datos cuando le apetezca: perfecto para actualizaciones en vivo. El esquema de URL es `ws://` (o `wss://` sobre TLS) en lugar de `http://`.

::: listing lumen/web/balance_ws_controller.py | Listado 18.8 — Flujo de saldo en vivo vía WebSocket
import asyncio

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from pyfly.websocket import WebSocketSession, websocket_mapping


@rest_controller
@request_mapping("/ws")
class BalanceFeedController:
    """Streams balance updates to connected clients."""

    def __init__(self, wallet_service) -> None:
        self._wallet = wallet_service
        self._clients: set[WebSocketSession] = set()

    @websocket_mapping("/balance/{wallet_id}")
    async def balance_feed(self, session: WebSocketSession) -> None:
        wallet_id = session.path_params["wallet_id"]
        await session.accept()
        self._clients.add(session)
        try:
            while True:
                balance = await self._wallet.get_balance(wallet_id)
                await session.send_json(
                    {"wallet_id": wallet_id, "balance_minor": balance}
                )
                await asyncio.sleep(1)
        finally:
            self._clients.discard(session)

    async def on_disconnect(self, session: WebSocketSession) -> None:
        self._clients.discard(session)
:::

**Cómo funciona.** `@websocket_mapping("/balance/{wallet_id}")` monta el endpoint en `ws://<host>/ws/balance/{wallet_id}`. La ruta completa es la base `@request_mapping` del controlador (`/ws`) concatenada con la ruta del decorador.

!!! tip "Ejecútalo"
    Arranca la app y luego abre el flujo en vivo con cualquier cliente
    WebSocket. Usando `websocat` (`brew install websocat`):

    ```bash
    uv run pyfly run
    # en otra terminal:
    websocat ws://localhost:8080/ws/balance/w-001
    ```

    Deberías ver llegar un frame JSON aproximadamente una vez por segundo,
    empujado por el servidor sin que vuelvas a pedirlo:

    ```json
    {"wallet_id": "w-001", "balance_minor": 5000}
    {"wallet_id": "w-001", "balance_minor": 5000}
    ```

    Deposita en `w-001` desde otra terminal y observa cómo el valor de
    `balance_minor` salta en el siguiente frame: sin sondeo, sin refresco. Pulsa
    `Ctrl+C` para cerrar; se ejecuta el `on_disconnect` del controlador y la
    sesión se elimina del conjunto de difusión.

`WebSocketSession` expone el ciclo de vida de la conexión:

| Método | Descripción |
|---|---|
| `await accept(subprotocol=None)` | Completar el handshake |
| `await send_json(data)` | Serializar y enviar un mensaje JSON |
| `await send_text(data)` | Enviar una cadena plana |
| `await receive_text()` | Bloquear hasta que llegue un mensaje de texto |
| `await receive_json()` | Bloquear hasta que llegue un mensaje JSON |
| `await close(code=1000, reason=None)` | Cerrar la conexión limpiamente |

`session.path_params`, `session.query_params` y `session.headers` exponen los metadatos de la conexión. Las rutas WebSocket se descubren automáticamente junto con las rutas HTTP: no se requiere configuración adicional.

El método opcional `on_disconnect` lo invoca automáticamente el registrador después de que el manejador `@websocket_mapping` retorne o el cliente cierre la conexión (solo si la conexión se aceptó antes), dando a los controladores un lugar seguro donde liberar recursos.

!!! tip "Difusión (broadcasting)"
    Mantén un `set[WebSocketSession]` en el controlador y difunde con
    `for client in list(self._clients): await client.send_json(payload)`.
    Como los beans de los controladores son singletons, el conjunto vive
    durante toda la vida de la aplicación.

---

## Comandos de shell y runners de arranque

No toda funcionalidad vive detrás de un endpoint HTTP. Los scripts de siembra de bases de datos, las migraciones de datos puntuales y los trabajos por lotes programados se expresan mejor como comandos CLI que se ejecutan dentro del mismo contenedor de DI, compartiendo servicios, configuración y repositorios con la aplicación principal.

La idea clave aquí: un **comando de shell** es solo un método que se ejecuta desde la terminal pero que aún tiene acceso completo a los servicios cableados de tu aplicación. No escribes un script aparte que recree la conexión a la base de datos a mano: el comando recibe el mismo `WalletService` que usan tus controladores HTTP, porque vive dentro del mismo contenedor de DI.

### @shell_component y @shell_method

::: listing lumen/cli/wallet_commands.py | Listado 18.9 — Comandos de shell con DI
from pyfly.shell import (
    shell_argument,
    shell_component,
    shell_method,
    shell_option,
)


@shell_component
class WalletCommands:
    """Operational commands for the Lumen wallet service."""

    def __init__(self, wallet_service) -> None:
        self._wallet = wallet_service

    @shell_method(group="wallet", help="Deposit funds into a wallet")
    @shell_argument("wallet_id", help="Target wallet identifier")
    @shell_option("--amount", help="Amount in minor units (integer cents)")
    async def deposit(
        self, wallet_id: str, amount: int = 100
    ) -> str:
        result = await self._wallet.deposit(wallet_id, amount)
        return f"New balance: {result['balance_minor']} minor units"

    @shell_method(group="wallet", help="Show current balance")
    @shell_argument("wallet_id", help="Wallet to inspect")
    async def balance(self, wallet_id: str) -> str:
        data = await self._wallet.get_balance(wallet_id)
        return f"{wallet_id}: {data['balance_minor']} minor units"
:::

Habilita el shell en `pyfly.yaml`:

```yaml
pyfly:
  shell:
    enabled: true
```

PyFly autoconfigura un `ClickShellAdapter` y cablea cada `@shell_method` al arranque. El nombre del grupo se convierte en un subcomando:

```bash
python -m lumen wallet deposit w-001 --amount 500
python -m lumen wallet balance w-001
python -m lumen        # sin argumentos → entra en modo REPL
```

!!! tip "Ejecútalo"
    Deposita 500 unidades menores en un monedero directamente desde la línea de
    comandos: el comando ejecuta tu `WalletService` real contra la base de datos
    real:

    ```bash
    uv run python -m lumen wallet deposit w-001 --amount 500
    ```

    Salida esperada (el valor de retorno de tu método `deposit`, impreso por
    el adaptador de shell):

    ```
    New balance: 500 minor units
    ```

    Ejecuta `uv run python -m lumen wallet balance w-001` y verás reflejado de
    vuelta el mismo `500`: prueba de que el comando y la API HTTP hablan con el
    mismo estado persistente, no con una copia desechable en memoria.

### CommandLineRunner: tareas puntuales tras el arranque

Para tareas que se ejecutan una vez al arranque —siembra, calentamiento, comprobaciones de conexión— implementa **`CommandLineRunner`**:

::: listing lumen/runners/seed_runner.py | Listado 18.10 — Sembrador de base de datos tras el arranque
from pyfly.container import service
from pyfly.shell import CommandLineRunner


@service
class SeedRunner(CommandLineRunner):
    """Seed the database with a default admin wallet on first boot."""

    def __init__(self, wallet_service) -> None:
        self._wallet = wallet_service

    async def run(self, args: list[str]) -> None:
        if "--seed" in args:
            await self._wallet.ensure_default_wallet()
            print("Default wallet ensured.")
:::

Cualquier bean cuya clase implemente `async def run(self, args: list[str]) -> None` satisface estructuralmente el protocolo `CommandLineRunner`. El framework lo detecta vía `isinstance()` (el protocolo es `@runtime_checkable`) después de que se dispare `ApplicationReadyEvent`, y luego lo invoca con los argumentos CLI en crudo. Usa `@order(n)` para controlar el orden de ejecución cuando coexisten varios runners.

!!! tip "Ejecútalo"
    El runner se dispara durante el arranque de la aplicación y recibe el
    `sys.argv[1:]` del proceso, así que lanza la app a través de su punto de
    entrada CLI con el flag añadido:

    ```bash
    uv run python -m lumen --seed
    ```

    Tras el banner y la tabla de rutas, verás la línea de confirmación del
    runner:

    ```
    Default wallet ensured.
    ```

    Arranca sin `--seed` y la línea no aparece: la guarda `if "--seed" in args`
    se salta el trabajo. Esa es la diferencia entre un *runner* (se ejecuta en
    cada arranque, lo controlas tú) y un comando de shell puntual (se ejecuta
    solo cuando invocas su nombre).

!!! spring "Equivalencia con Spring"
    `@shell_component`, `@shell_method`, `@shell_option`,
    `@shell_argument` y `CommandLineRunner` son equivalentes
    directos de `@ShellComponent`, `@ShellMethod`, `@ShellOption`,
    `@ShellArgument` de Spring Shell y de la interfaz `CommandLineRunner`
    de Spring Boot. Click reemplaza a JLine como librería de terminal,
    pero el modelo de programación es idéntico.

---

## Generar un SDK a partir de la especificación OpenAPI

Cuando Lumen expone una API HTTP, los servicios posteriores deberían llamarla mediante un cliente generado, no mediante llamadas `httpx` escritas a mano que se desincronizan. PyFly construye y sirve una especificación OpenAPI 3.1 automáticamente en `/openapi.json`.

Una **especificación OpenAPI** es una descripción legible por máquina de tu API HTTP: cada ruta, cada parámetro, cada forma de petición y respuesta. Un **SDK** (software development kit) es la librería cliente que una herramienta genera *a partir de* esa especificación: métodos tipados que envuelven las llamadas HTTP por ti. La cadena es: tus controladores → la especificación → un cliente generado. Como cada eslabón es mecánico, el cliente nunca puede desincronizarse silenciosamente del servidor.

`OpenAPIGenerator` ensambla la especificación a partir de los metadatos de ruta recopilados por `ControllerRegistrar`:

- **Info**: poblada a partir de `title`, `version` y `description` pasados a `create_app()`.
- **Paths**: una operación por cada `@get_mapping` / `@post_mapping`, etc., con parámetros inferidos a partir de las anotaciones de tipo `PathVar[T]`, `QueryParam[T]`, `Header[T]` y `Body[BaseModel]`.
- **Schemas**: modelos de Pydantic registrados en `components.schemas` vía `model_json_schema()` y referenciados con `$ref`.

Con la especificación disponible, genera un cliente Python en dos pasos: descarga la especificación y luego ejecuta el generador.

**Paso 1: descarga la especificación de una instancia en ejecución.** La especificación vive en el puerto de aplicación (`8080`), junto a tus rutas de negocio.

**Paso 2: ejecuta el generador de OpenAPI** para convertir ese JSON en un paquete Python instalable.

```bash
# Download the spec from a running instance
curl http://localhost:8080/openapi.json -o lumen-spec.json

# Generate a Python client package
openapi-generator-cli generate \
  -i lumen-spec.json \
  -g python \
  -o lumen-client \
  --package-name lumen_client
```

El paquete `lumen_client` generado contiene modelos tipados y un `DefaultApi` con un método por operación. Los servicios consumidores lo añaden como dependencia y lo llaman sin saber nada de HTTP:

::: listing payment/services/wallet_client.py | Listado 18.11 — Consumir el SDK de Lumen generado
from lumen_client import ApiClient, Configuration, DefaultApi


class WalletGateway:
    """Typed façade over the generated Lumen client SDK."""

    def __init__(self, base_url: str) -> None:
        cfg = Configuration(host=base_url)
        self._api = DefaultApi(ApiClient(cfg))

    def get_balance(self, wallet_id: str) -> int:
        result = self._api.get_wallet_balance(wallet_id)
        return result.balance_minor
:::

!!! tip "Mantén la especificación versionada"
    Incluye `lumen-spec.json` en el repositorio de Lumen y regenera los
    paquetes cliente en CI cada vez que cambie la especificación. Los
    equipos posteriores se fijan a una versión concreta de la
    especificación a través de su gestor de dependencias: la misma
    disciplina que usan los equipos Java con las versiones de artefactos
    de Maven.

---

## Llevarlo a producción

### Empaquetar con Docker

**Contenerizar**, en términos sencillos, significa congelar tu app y todo lo que necesita para ejecutarse —Python, dependencias, tu código, tu configuración— en una única imagen que se ejecuta de forma idéntica en tu portátil, en CI y en producción. Un `Dockerfile` es la receta para construir esa imagen.

`pyfly new` genera un `Dockerfile` para cada arquetipo. Para un servicio web tiene este aspecto tras el endurecimiento de producción. Léelo de arriba abajo: cada línea es un paso del build:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && \
    uv sync --no-dev --extra web --extra data-relational \
             --extra security --extra observability

COPY src/ src/
COPY pyfly.yaml .

# 8080 = application traffic; 9090 = the management port (actuator + admin).
EXPOSE 8080 9090
CMD ["pyfly", "run", "--host", "0.0.0.0", \
     "--port", "8080", "--server", "granian", "--workers", "2"]
```

Recorriendo la receta: `FROM` escoge una imagen base de Python pequeña; `COPY` + `uv sync` instalan exactamente los extras de dependencias que nombras (y nada más); el segundo `COPY` añade tu fuente y tu configuración; `EXPOSE` documenta los dos puertos en los que escucha el contenedor; `CMD` es el comando que se ejecuta cuando arranca el contenedor. El flag `--port 8080` de aquí establece `pyfly.server.port` para este proceso: el puerto de gestión se queda en su valor por defecto `9090` salvo que anules `pyfly.management.server.port`.

Instala solo los extras que tu servicio realmente usa: el meta-extra `full` arrastra los drivers de Kafka, RabbitMQ y MongoDB incluso cuando no necesitas ninguno de ellos.

!!! tip "Ejecútalo"
    Construye la imagen y ejecútala, mapeando ambos puertos fuera del
    contenedor:

    ```bash
    docker build -t lumen:local .
    docker run --rm -p 8080:8080 -p 9090:9090 lumen:local
    ```

    Luego confirma que ambos listeners responden: el puerto de negocio en
    `8080` y el puerto de gestión en `9090`:

    ```bash
    curl http://localhost:8080/openapi.json   # app: returns the spec
    curl http://localhost:9090/actuator/health # management: {"status":"UP"}
    ```

    Dos puertos, un proceso. Esa separación es sobre la que se construye el
    resto de esta sección.

### Variables de entorno y secretos

Nunca incrustes secretos en `pyfly.yaml`. PyFly resuelve marcadores de posición `${ENV_VAR}` en cualquier parte de la configuración:

```yaml
pyfly:
  data:
    relational:
      url: ${DATABASE_URL}

  security:
    jwt:
      secret: ${JWT_SECRET}
```

PyFly lee los valores reales del entorno del contenedor al arranque. En Kubernetes, respalda esas variables con un `Secret`; en Docker Compose, usa un archivo `.env` que nunca se commitea. El comando `pyfly doctor` comprueba que las herramientas requeridas están presentes, pero no valida secretos: esa responsabilidad sigue siendo tuya.

### Apagado ordenado

PyFly respeta el apagado ordenado por defecto. Establece `pyfly.server.graceful-timeout` (en segundos) para controlar cuánto espera el servidor a que se completen las peticiones en vuelo antes de forzar la salida:

```yaml
pyfly:
  server:
    graceful-timeout: 30
```

SIGTERM dispara la secuencia de apagado: el servidor deja de aceptar nuevas conexiones, `ApplicationContext.stop()` ejecuta los hooks `@pre_destroy` y `stop_all()` para los plugins, y el proceso sale limpiamente. En Kubernetes, establece `terminationGracePeriodSeconds` al menos cinco segundos por encima de `graceful-timeout`.

### Selección de servidor

El servidor ASGI se selecciona por prioridad en tiempo de ejecución:

| Prioridad | Servidor | Extra de instalación |
|---|---|---|
| 1 | Granian (Rust/tokio) | `granian` |
| 2 | Uvicorn | `web` (por defecto) |
| 3 | Hypercorn | `hypercorn` |

Para producción, prefiere Granian: ofrece aproximadamente 3× el rendimiento de Uvicorn con HTTP/2 nativo. Combínalo con uvloop en Linux para un aceleramiento adicional de 2 a 4× del bucle de eventos:

```bash
uv add "pyfly[web-fast]"   # granian + uvloop in one shot
```

Fija la elección en YAML para evitar sorpresas en máquinas donde resulta que hay varios servidores instalados:

```yaml
pyfly:
  server:
    type: granian
    event-loop: uvloop
    workers: 4
    graceful-timeout: 30
```

### El despliegue de dos puertos

Este es el hecho de despliegue que más necesitas interiorizar para v26.6.110. PyFly se ejecuta en **dos puertos**, ambos dentro de un único proceso:

- el **puerto de aplicación** —`pyfly.server.port`, por defecto `8080`— sirve tu API de negocio, tus flujos WebSocket, la especificación OpenAPI y las rutas del config-server;
- el **puerto de gestión** —`pyfly.management.server.port`, por defecto `9090`— sirve el Actuator (`/actuator/*`) y el panel de administración (`/admin`).

El puerto de gestión es un segundo listener en proceso, no procesos worker adicionales, así que casi no cuesta nada. Dos opciones de ajuste importan en el momento del despliegue: establece `pyfly.management.server.port` **igual** a `pyfly.server.port` para colapsar todo en un único puerto, o ponlo a **`-1`** para deshabilitar por completo los endpoints web de gestión. La anulación por entorno es `PYFLY_MANAGEMENT_SERVER_PORT`.

¿Por qué separarlos? Porque te permite exponer solo `8080` a internet manteniendo las comprobaciones de salud, los scrapes de Prometheus y la consola de administración en `9090`, accesibles únicamente desde dentro de tu clúster.

!!! warning "El puerto de gestión está ABIERTO por defecto"
    A partir de v26.6.110, el puerto de gestión está **sin autenticación por
    defecto** (el modelo `management.server.port` de Spring Boot): cualquier
    cosa que pueda alcanzar `9090` puede leer `/actuator/*` y `/admin`. Eso es
    intencionado: el puerto está pensado para situarse tras aislamiento de red,
    nunca en el internet público. Si no puedes garantizar ese aislamiento,
    aplica también los filtros de seguridad de la app al puerto de gestión:

    ```yaml
    pyfly:
      management:
        security:
          enabled: true
    ```

    Con ese flag activado, la misma autenticación, las guardas de roles y las
    reglas CSRF que protegen tu API de negocio también protegen `9090`.

### Endpoints de salud

PyFly expone endpoints de actuator al estilo de Spring Boot de fábrica. Viven en el **puerto de gestión** (`9090`):

| Endpoint | Propósito |
|---|---|
| `GET /actuator/health` | Salud agregada (UP / DOWN) |
| `GET /actuator/health/liveness` | Sonda de liveness de Kubernetes |
| `GET /actuator/health/readiness` | Sonda de readiness de Kubernetes |
| `GET /actuator/metrics` | Desglose por métrica (p. ej. `/actuator/metrics/http.server.requests`) |
| `GET /actuator/prometheus` | Endpoint de scrape de Prometheus |

Solo `health` e `info` se exponen sobre HTTP por defecto (el valor por defecto seguro de Spring Boot). Para publicar las métricas y el endpoint de scrape de Prometheus, añádelos a la lista de exposición en `pyfly.yaml`:

```yaml
pyfly:
  management:
    endpoints:
      web:
        exposure:
          include: health,info,metrics,prometheus
    endpoint:
      health:
        show-details: when-authorized
```

!!! tip "Ejecútalo"
    Golpea las sondas en el puerto de gestión: responden de inmediato porque
    ese puerto está abierto por defecto:

    ```bash
    curl http://localhost:9090/actuator/health
    curl http://localhost:9090/actuator/health/readiness
    ```

    Esperado: un objeto de estado JSON que el orquestador puede analizar:

    ```json
    {"status": "UP"}
    ```

    Una comprobación de readiness que falla (una base de datos aún
    calentándose, por ejemplo) devuelve `{"status": "DOWN"}` con HTTP `503`,
    que es exactamente lo que Kubernetes necesita para retener el tráfico hasta
    que el pod esté listo.

Luego conecta tu deployment de Kubernetes: fíjate en que cada sonda y cada scrape apuntan al **puerto de gestión** `9090`, no a `8080`:

```yaml
livenessProbe:
  httpGet:
    path: /actuator/health/liveness
    port: 9090
  initialDelaySeconds: 10
  periodSeconds: 15
readinessProbe:
  httpGet:
    path: /actuator/health/readiness
    port: 9090
  initialDelaySeconds: 5
  periodSeconds: 10
```

!!! spring "Equivalencia con Spring"
    La división de dos puertos es un port directo del `management.server.port`
    de Spring Boot: tráfico de la app en `server.port`, actuator y la UI de
    gestión en un `management.server.port` dedicado. La postura de abierto por
    defecto, la convención de `-1` para deshabilitar y `management.security.enabled`
    para bloquearlo reflejan todos el comportamiento de Spring Boot.

!!! note "Sondas personalizadas desde el HealthAggregator en vivo"
    Nuevo en v26.6.110, el `HealthAggregator` en vivo es accesible en
    `app.state.pyfly_health_aggregator` (solo adaptador Starlette). Registra un
    indicador de readiness adicional después de `create_app()` —por ejemplo una
    comprobación que haga ping a un servicio posterior— y aparece en
    `/actuator/health` independientemente de si el actuator se ejecuta en el
    puerto de gestión compartido o en el separado.

**Lo que acaba de ocurrir.** Ahora tienes la forma completa de un despliegue de
PyFly: un contenedor, dos puertos (`8080` para usuarios, `9090` para
operaciones), secretos inyectados como variables de entorno, un servidor Granian
ajustado con workers explícitos, una ventana de apagado ordenado que drena las
peticiones en vuelo y sondas de Kubernetes conectadas al puerto de gestión. La
lista de comprobación de abajo es la misma imagen en forma de lista que puedes
pegar en una plantilla de pull request.

### La lista de comprobación de producción

- [ ] Todos los secretos son variables de entorno: ninguno está en
      `pyfly.yaml` ni en el control de versiones.
- [ ] La imagen Docker instala solo los extras que el servicio usa.
- [ ] El servidor está fijado a Granian + uvloop; `workers` está
      establecido explícitamente (no dejado en `1` para máquinas
      multinúcleo).
- [ ] `graceful-timeout` es de al menos 15 s; el
      `terminationGracePeriodSeconds` de Kubernetes es al menos 5 s más.
- [ ] Las sondas de liveness y readiness apuntan al **puerto de gestión**
      (`9090`), no al puerto de aplicación, y están probadas.
- [ ] `/actuator/health` (en `9090`) devuelve UP antes de que se envíe tráfico.
- [ ] El puerto de gestión está aislado a nivel de red, o
      `pyfly.management.security.enabled: true` está establecido: está abierto
      por defecto.
- [ ] El endpoint de scrape de Prometheus (`/actuator/prometheus` en `9090`)
      está añadido a la lista de exposición y lo scrapea el stack de
      monitorización.
- [ ] El logging estructurado está habilitado (`pyfly[observability]`) y el
      nivel de log es `INFO` en producción, no `DEBUG`.
- [ ] El exportador de OpenTelemetry apunta al colector de producción.
- [ ] Las migraciones de base de datos (`pyfly db upgrade`) se ejecutan en un
      paso previo al despliegue, no al arranque de la aplicación.
- [ ] La especificación OpenAPI generada está versionada en CI y los paquetes
      SDK posteriores se fijan a una revisión concreta de la especificación.
- [ ] `pyfly doctor` pasa en cada máquina de desarrollo y en cada runner de CI.

---

## Lo que construiste {.recap}

Hace diecisiete capítulos escribiste `@pyfly_application` y viste cómo el contenedor de DI cableaba tu primer `@service`. Hoy ese mismo contenedor impulsa una plataforma de monedero en producción. Esto es lo que construiste por el camino.

**Capítulos 1–3** te dieron los cimientos: un contenedor de DI de primera clase, configuración flexible (YAML, env, perfiles, expresiones SpEL) y una capa HTTP completa con request mapping, filtros, negociación de contenido y una capa de serialización JSON que puedes reemplazar sin tocar un solo controlador.

**Capítulos 4–6** introdujeron la persistencia. Mapeaste entidades con SQLAlchemy, gestionaste la evolución del esquema con Alembic y estructuraste el dominio con patrones tácticos de DDD: agregados, objetos de valor y repositorios que mantienen la lógica de negocio fuera de la infraestructura.

**Capítulos 7–9** dieron vida a la arquitectura. CQRS separó las lecturas de las escrituras a nivel del manejador. EDA dejó que los servicios reaccionaran a eventos sin sondear. El event sourcing convirtió cada cambio de estado en un hecho de primera clase: reproducible, auditable y el cimiento de las proyecciones.

**Capítulos 10–12** enseñaron a Lumen a salir de su propio proceso. Clientes HTTP resilientes llamaban a servicios posteriores sin fallos en cascada. Las sagas orquestaban transacciones de múltiples pasos a través de las fronteras de los servicios con compensación automática cuando algún paso salía mal.

**Capítulos 13–15** endurecieron la plataforma. La caché redujo la presión sobre la base de datos. Limitadores de tasa, bulkheads, tiempos de espera y cortacircuitos (circuit breakers) convirtieron cada dependencia en un radio de impacto controlado. Las trazas distribuidas, el logging estructurado y un panel de administración en vivo te dieron ojos dentro del sistema en cada capa.

**Capítulos 16–17** cerraron el bucle de retroalimentación. Un conjunto de pruebas estructurado —pruebas unitarias, de integración y de persistencia respaldadas por Testcontainers— hizo seguro cambiar la plataforma. Tareas programadas, notificaciones push, webhooks y callbacks dejaron que Lumen llegara al mundo según su propio calendario.

**Capítulo 18** te mostró lo que hay más allá del núcleo: un sistema de plugins para la extensión abierta, un motor de reglas YAML para la lógica propiedad del negocio, un Config Server para la configuración de toda la flota, i18n para audiencias globales, WebSocket para UX en tiempo real, un módulo Shell para herramientas operativas, una especificación OpenAPI que genera SDKs cliente tipados automáticamente y los hábitos de producción que mantienen todo eso en marcha, empaquetado como un único contenedor que escucha en dos puertos, `8080` para usuarios y `9090` para operaciones.

PyFly no es magia. Toda abstracción de este libro tiene un coste, y ahora entiendes cuál es ese coste: un contenedor de DI que arranca en milisegundos pero te obliga a pensar en los ámbitos de los beans; un servidor HTTP asíncrono que maneja miles de conexiones concurrentes pero te obliga a evitar llamadas bloqueantes; un motor de sagas que sobrevive a fallos parciales pero te obliga a escribir transacciones de compensación.

Entender el coste es lo que separa a un practicante de un lector que se limita a copiar patrones. Ahora eres un practicante.

---

## Pruébalo tú mismo {.exercises}

1. **Añade un plugin personalizado.** Implementa un `AuditPlugin` que aporte una extensión a un punto de extensión `"audit-sinks"`. La clase de extensión debe implementar una interfaz `AuditSink` y escribir cada evento en un archivo. En una prueba, llama a `PluginManager.registry.get("audit-sinks")` y afirma que tu extensión se devuelve con el `name` esperado.

2. **Despliega un cambio de reglas sin redesplegar.** Almacena `transaction_rules.yaml` en el Config Server (`pyfly.config-server.enabled: true`). Escribe un `RiskService` que obtenga el YAML vía `ConfigClient` en cada llamada a `assess()` (o que lo cachee con un TTL corto). Actualiza el umbral `value` a través de la ruta `POST /{app}/{profile}` y verifica que `assess()` recoge el nuevo valor sin reiniciar el servicio.

3. **Localiza un mensaje de rechazo.** Añade `wallet.limit_exceeded` a `i18n/messages_en.yaml` e `i18n/messages_es.yaml`. Conecta un `NotificationService` que lea el locale de la cabecera `Accept-Language` y devuelva la cadena correcta. Escribe dos pruebas —una con `Accept-Language: en`, otra con `Accept-Language: es`— y afirma que cada una devuelve el mensaje adecuado.

---

Lumen está lista para producción. Para lo que viene a continuación —nuevos módulos, plugins de la comunidad y notas de versión— visita la documentación del framework en [github.com/fireflyframework/fireflyframework-pyfly](https://github.com/fireflyframework/fireflyframework-pyfly). Cada concepto de este libro vive en ese repositorio; el código fuente es la referencia definitiva.
