<span class="eyebrow">Capítulo 4</span>

# Tu primera API HTTP {.chtitle}

::: figure art/openers/ch04.svg | &nbsp;

Lumen tiene los servicios cableados, una historia de configuración limpia y un
ciclo de vida que abarca desde el desarrollo hasta la producción. Lo único que
falta es una forma de que el mundo exterior hable con él. Este capítulo cierra
la Parte I convirtiendo el dominio del monedero en una API REST limpia y
validada: una con documentación OpenAPI automática, respuestas de error
estructuradas en las que los clientes pueden confiar y las convenciones
gestionadas por el framework que ya esperas del resto de PyFly.

---

## Controladores y mapeos de rutas

Todo framework web debe responder a dos preguntas: ¿cómo encuentra una petición
el manejador correcto, y cómo obtiene ese manejador las dependencias que
necesita? Los frameworks que responden a estas preguntas con mecanismos
separados te obligan a mantener un fichero de router, un fichero de pegamento de
DI y un andamiaje de documentación en tres sitios distintos. PyFly colapsa las
tres preocupaciones en una sola clase.

Un **controlador** en PyFly es una clase Python corriente que el contenedor de
DI gestiona y a la que la capa web enruta las peticiones. Dos decoradores lo
marcan: `@rest_controller`, de `pyfly.container`, lo registra como bean y fija su
estereotipo; `@request_mapping`, de `pyfly.web`, fija el prefijo de URL que
hereda cada manejador de la clase.

Algunos términos de ese párrafo reaparecerán a lo largo del capítulo, así que
fijémoslos de una vez. Un **bean** es simplemente un objeto que el contenedor de
DI crea y te entrega: tú nunca llamas a `WalletController()`; el framework lo
construye y conserva una única instancia compartida. Un **estereotipo** es una
etiqueta que el framework estampa en una clase para saber *qué tipo* de bean es:
`@rest_controller` estampa «esto es un controlador web», que es la señal que la
maquinaria de arranque utiliza para ir a buscar rutas dentro de él. Un
**manejador** (handler) es un método `async def` del controlador que responde a
un tipo de petición. Con esas tres palabras en la mano, el resto del capítulo se
lee como prosa llana.

Los manejadores de ruta son simples métodos `async def`, cada uno decorado con
`@get_mapping`, `@post_mapping`, `@put_mapping`, `@patch_mapping` o
`@delete_mapping`. Cada decorador de mapeo acepta una ruta relativa opcional y un
`status_code` opcional. La URL completa es la ruta base de `@request_mapping`
concatenada con la ruta relativa del decorador del método.

### Un ejemplo de lectura previo a CQRS

El Capítulo 7 introduce el bus completo de comandos/consultas CQRS que Lumen usa
en producción. Aquí, en la Parte I, la mecánica de la capa web se enseña sobre el
mismo dominio del monedero, respaldado por un almacén en memoria simple en lugar
del bus. La estructura del controlador, los imports y las formas de los
decoradores son *idénticos* a los del Capítulo 7; solo cambia el destino del
despacho.

::: listing lumen/web/controllers/wallet_controller.py | Listado 4.1 — WalletController usando los decoradores web reales de PyFly
from __future__ import annotations

from pyfly.container import rest_controller
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import (
    Body,
    PathVar,
    QueryParam,
    Valid,
    get_mapping,
    post_mapping,
    request_mapping,
)

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto


# ---------------------------------------------------------------------------
# In-memory store (replaced by a database repository in Chapter 5)
# ---------------------------------------------------------------------------
_wallets: dict[str, WalletDto] = {}


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, inspect.

    In Part I the controller holds a minimal in-memory store so you can
    focus on the web-layer mechanics — decorators, binding, validation, and
    error handling — without persistence or CQRS machinery. Chapter 7
    replaces the store with DefaultCommandBus / DefaultQueryBus dispatching.
    """

    @post_mapping("", status_code=201)
    async def open_wallet(
        self, request: Valid[Body[OpenWalletRequest]]
    ) -> dict[str, str]:
        import uuid
        wallet_id = str(uuid.uuid4())
        wallet = WalletDto(
            id=wallet_id,
            owner_id=request.owner_id,
            currency=request.currency,
            balance_minor=0,
            balance=0.0,
            created_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
        )
        _wallets[wallet_id] = wallet
        return {"wallet_id": wallet_id}

    @get_mapping("/{wallet_id}")
    async def get_wallet(self, wallet_id: PathVar[str]) -> WalletDto:
        result = _wallets.get(wallet_id)
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("/{wallet_id}/balance")
    async def get_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        wallet = _wallets.get(wallet_id)
        if wallet is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return BalanceDto(
            id=wallet.id,
            currency=wallet.currency,
            balance_minor=wallet.balance_minor,
            balance=wallet.balance,
        )

    @post_mapping("/{wallet_id}/deposit")
    async def deposit(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
    ) -> dict[str, int | str]:
        wallet = _wallets.get(wallet_id)
        if wallet is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        new_balance = wallet.balance_minor + request.amount
        _wallets[wallet_id] = wallet.model_copy(
            update={
                "balance_minor": new_balance,
                "balance": new_balance / 100,
            }
        )
        return {"wallet_id": wallet_id, "balance_minor": new_balance}

    @get_mapping("")
    async def list_wallets(
        self,
        owner_id: QueryParam[str] = None,
    ) -> list[WalletDto]:
        wallets = list(_wallets.values())
        if owner_id is not None:
            wallets = [w for w in wallets if w.owner_id == owner_id]
        return wallets
:::

Vale la pena examinar cuatro decisiones de diseño de este listado.

`@rest_controller` hace dos cosas a la vez: registra `WalletController` como bean
singleton en el contenedor de DI y fija el marcador `__pyfly_stereotype__` que
`ControllerRegistrar` utiliza para descubrir y montar rutas en el arranque.
Emparejarlo con `@request_mapping("/api/v1/wallets")` significa que cada decorador
a nivel de método hereda ese prefijo: escribes la ruta base una sola vez.

Esta versión no tiene ningún `__init__` con colaboradores inyectados. El
diccionario en memoria `_wallets` es un almacén a nivel de módulo que basta para
la Parte I. El Capítulo 5 introduce los repositorios; el Capítulo 7 muestra el
patrón de producción: un constructor que recibe `DefaultCommandBus` y
`DefaultQueryBus` del contenedor de DI, despachando comandos y consultas a través
del bus en lugar de leer `_wallets` directamente.

Cada manejador devuelve un modelo Pydantic (`WalletDto`, `BalanceDto`) o un
`dict` corriente. El framework serializa el valor de retorno a JSON y fija la
cabecera `Content-Type`: el manejador nunca construye un objeto de respuesta. El
argumento `status_code=201` de `@post_mapping` produce un 201 Created en caso de
éxito; todos los demás manejadores usan 200 por defecto.

Los cinco decoradores de mapeo aceptan los mismos dos parámetros:

| Parámetro | Por defecto | Descripción |
|---|---|---|
| `path` | `""` | Ruta relativa añadida a la base. Usa `{name}` para variables de ruta. |
| `status_code` | `200` | Código de estado HTTP para una respuesta correcta. |

`@post_mapping("", status_code=201)` mapea `POST /api/v1/wallets` y devuelve 201
en caso de éxito. `@get_mapping("/{wallet_id}")` mapea
`GET /api/v1/wallets/{wallet_id}`. Las rutas se concatenan en el arranque; las
barras duplicadas o finales se normalizan automáticamente.

### Construir el controlador, paso a paso

Si estás escribiendo esto desde cero, el listado anterior aterriza todo de golpe.
Aquí tienes el mismo controlador ensamblado en el orden en que realmente lo
construirías, para que cada decorador tenga un trabajo que hacer antes de que
llegue el siguiente.

**Paso 1 — Crea el fichero y la clase.** Crea
`src/lumen/web/controllers/wallet_controller.py` y define una clase vacía
decorada con los dos decoradores a nivel de clase. Esto basta para que PyFly
descubra el controlador en el arranque, incluso antes de que tenga una sola ruta.

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, inspect."""
```

**Paso 2 — Añade el primer manejador.** Dale a la clase un método `async def` y
márcalo con un decorador de mapeo. `@post_mapping("", status_code=201)` mapea
`POST /api/v1/wallets` —la ruta vacía significa «la ruta base sin nada
añadido»— y promete un `201 Created` en caso de éxito.

**Paso 3 — Añade los manejadores restantes.** Repite el patrón: un `async def`
por ruta, cada uno con su propio decorador de mapeo y ruta relativa. El conjunto
completo del Listado 4.1 te da abrir, obtener, saldo, depósito y listar.

**Paso 4 — Conecta el almacén.** El diccionario `_wallets` a nivel de módulo es
la única «base de datos» que necesita la Parte I. Cada manejador lee de él y
escribe en él directamente; el Capítulo 5 lo cambia por un repositorio real sin
tocar un solo decorador.

!!! note "Nota"
    Fíjate en lo que *no* escribiste: ningún fichero de router que mapee URLs a
    funciones, ninguna llamada de registro en `main.py`, ninguna entrada manual
    de OpenAPI. Los decoradores son el registro. En el arranque,
    `ControllerRegistrar` encuentra cada bean `@rest_controller` y monta sus
    rutas por ti.

!!! tip "Pruébalo"
    Arranca el servidor y confirma que las rutas están activas. Desde la raíz
    del proyecto:

    ```bash
    uv run pyfly run --server uvicorn
    ```

    El banner de arranque informa de la versión del framework y del puerto al
    que está enlazado:

    ```
    :: PyFly Framework :: (v26.06.110) (Python 3.13.13)
    ```

    En una segunda terminal, abre un monedero y vuelve a leerlo:

    ```bash
    curl -s -X POST localhost:8080/api/v1/wallets \
      -H 'Content-Type: application/json' \
      -d '{"owner_id": "alice", "currency": "EUR"}'
    ```

    Deberías ver un cuerpo `201` con el id generado:

    ```json
    {"wallet_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
    ```

    Copia ese id y obtén el monedero:

    ```bash
    curl -s localhost:8080/api/v1/wallets/a1b2c3d4-e5f6-7890-abcd-ef1234567890
    ```

    ```json
    {"id": "a1b2c3d4-...", "owner_id": "alice", "currency": "EUR",
     "balance_minor": 0, "balance": 0.0, "created_at": "2026-06-15T10:30:00+00:00"}
    ```

**Qué acaba de pasar.** Dos decoradores a nivel de clase registraron el
controlador y fijaron su prefijo de URL; un decorador a nivel de método convirtió
un `async def` en una ruta activa; el framework hizo el enrutamiento, la
serialización JSON y la gestión del código de estado. Escribiste la intención de
negocio, no la fontanería.

::: figure art/figures/04-request.svg | Figura 4.1 — Cómo fluye una petición hasta tu manejador.

!!! spring "Equivalencia con Spring"
    `@rest_controller` + `@request_mapping` + `@get_mapping` / `@post_mapping`
    es una traducción directa de `@RestController` + `@RequestMapping` +
    `@GetMapping` / `@PostMapping` de Spring. Los métodos manejadores devuelven
    valores directamente (no `ResponseEntity`) y el framework los convierte a
    JSON: exactamente el patrón que Spring fomenta con `@ResponseBody` en
    `@RestController`.

---

## Vincular los datos de la petición

Una petición transporta datos en varios sitios a la vez: la ruta de la URL
identifica el recurso, la cadena de consulta lleva filtros y paginación, el
cuerpo lleva la carga útil y las cabeceras llevan metadatos. La mayoría de los
frameworks abordan esto con mecanismos separados, cada uno con sus propias
convenciones. PyFly los unifica bajo una sola idea: **las anotaciones de tipo
genéricas en los parámetros del manejador declaran de dónde vienen los datos**.

Este enfoque hace que las firmas de los manejadores se documenten a sí mismas. La
lista de parámetros de cualquier manejador te dice exactamente qué partes de la
petición lee y qué tipos espera, sin abrir un fichero de router ni consultar la
documentación.

En términos llanos, **vincular** (binding) es que el framework copia una pieza de
la petición entrante en uno de los parámetros de tu manejador, convirtiéndola al
tipo que pediste por el camino. Tú declaras *qué quieres y de dónde viene* con una
anotación de tipo; PyFly hace la extracción, el parseo y la coerción de tipos
antes de que se ejecute el cuerpo de tu método.

El `ParameterResolver` inspecciona la firma de cada manejador en el arranque y
construye un plan de resolución, de modo que no hay sobrecarga de introspección
por petición. Cinco tipos de vinculación cubren cada parte de una petición HTTP:

### PathVar[T] — variables de ruta

Extrae un segmento con nombre de la ruta de la URL. El nombre del parámetro debe
coincidir con un `{placeholder}` de la ruta.

```python
@get_mapping("/{wallet_id}")
async def get_wallet(self, wallet_id: PathVar[str]) -> WalletDto:
    ...

@get_mapping("/{wallet_id}/transactions/{txn_id}")
async def get_transaction(
    self,
    wallet_id: PathVar[str],
    txn_id: PathVar[str],
) -> dict:
    ...
```

`PathVar` coacciona el segmento de cadena en bruto a `T` automáticamente.
`PathVar[int]`, `PathVar[float]` y `PathVar[UUID]` funcionan todos: la coerción
llama a `int(value)`, `float(value)` y `UUID(value)` respectivamente.

### QueryParam[T] — parámetros de consulta

Extrae un valor de la cadena de consulta, con soporte para valores por defecto y
valores opcionales.

```python
@get_mapping("")
async def list_wallets(
    self,
    owner_id: QueryParam[str] = None,
    page: QueryParam[int] = 1,
    size: QueryParam[int] = 20,
) -> list[WalletDto]:
    ...
```

Un parámetro es **obligatorio** cuando no tiene un valor por defecto en Python y
su tipo no admite `None`. Un `QueryParam` obligatorio ausente lanza
`InvalidRequestException` (HTTP 400). Para hacer un parámetro opcional, dale un
valor por defecto o anótalo como `QueryParam[str | None]`.

!!! tip "Pruébalo"
    Con el servidor en marcha y al menos un monedero abierto, ejercita el
    manejador `list_wallets`: primero sin filtro, luego con el parámetro de
    consulta opcional `owner_id`:

    ```bash
    curl -s 'localhost:8080/api/v1/wallets'
    curl -s 'localhost:8080/api/v1/wallets?owner_id=alice'
    ```

    El primero devuelve todos los monederos; el segundo devuelve solo los de
    Alice. Como `owner_id` tiene un valor por defecto de `None`, omitirlo es
    perfectamente válido: ningún 400. La variable de ruta se comporta igual a la
    inversa: pide un id de monedero que no existe y obtendrás un `404` limpio,
    que la siguiente sección disecciona.

### Body[T] — cuerpo de la petición

Deserializa el cuerpo de la petición JSON (o XML). Cuando `T` es un `BaseModel`
de Pydantic, se llama a `model_validate_json()` automáticamente.

```python
@post_mapping("", status_code=201)
async def open_wallet(
    self, request: Valid[Body[OpenWalletRequest]]
) -> dict[str, str]:
    ...
```

### Header[T] y Cookie[T]

Extraen valores de las cabeceras y cookies de la petición. Para las cabeceras, el
nombre del parámetro se convierte de `snake_case` a `kebab-case` automáticamente:

```python
@get_mapping("/me")
async def get_my_wallets(
    self,
    x_api_key: Header[str],
    session_id: Cookie[str | None],
) -> list[WalletDto]:
    ...
```

`x_api_key: Header[str]` lee la cabecera `x-api-key`. Una cabecera o cookie
obligatoria ausente lanza `InvalidRequestException` (HTTP 400), igual que un
parámetro de consulta ausente.

!!! tip "Consejo"
    Los cinco tipos de vinculación siguen la misma regla de **obligatorio frente
    a opcional**: sin valor por defecto + tipo que no admite `None` = obligatorio
    (HTTP 400 cuando está ausente); cualquier valor por defecto o `T | None` =
    opcional. La regla es uniforme en `QueryParam`, `Header` y `Cookie`: la
    aprendes una vez y se aplica en todas partes.

**Qué acaba de pasar.** Aprendiste todo el vocabulario de vinculación como cinco
anotaciones paralelas —`PathVar`, `QueryParam`, `Body`, `Header`, `Cookie`—
que se leen como prosa en la firma de un manejador y comparten una única regla de
obligatorio-frente-a-opcional. El framework lee la anotación, saca el valor del
sitio correcto, lo coacciona a tu tipo y te entrega un argumento listo para usar.
No hay nada más que cablear.

---

## Validación con Valid[T]

La vinculación le dice al framework *de dónde* vienen los datos. La validación le
dice *qué aspecto deben tener esos datos* antes de que tu manejador los vea
siquiera. Sin una capa que intercepte la entrada incorrecta pronto, la lógica de
validación se dispersa por los métodos de servicio, bloques `if` manuales
ensucian el código de negocio y distintos manejadores producen respuestas de
error inconsistentes según dónde resulte que capturen el problema.

PyFly resuelve esto a nivel de tipo. El `BaseModel` de Pydantic te da
restricciones a nivel de campo gratis. `Valid[T]` es el marcador de PyFly que
convierte un `ValidationError` de Pydantic en una **respuesta 422 estructurada**
en lugar de dejar que escale a un 500.

Una breve glosa antes del código. Un **DTO** —Data Transfer Object, objeto de
transferencia de datos— es una clase pequeña que describe la *forma* de los datos
que cruzan el cable: qué campos debe llevar una petición, o qué campos devolverá
una respuesta. Los DTO de Lumen son modelos Pydantic corrientes, de modo que las
declaraciones de campo hacen también de reglas de validación. La **validación** es
el acto de comprobar los datos entrantes contra esas reglas y rechazarlos
limpiamente si no encajan, antes de que se ejecute nada del código de tu
manejador.

### DTO de Pydantic para Lumen

Los DTO de petición y respuesta usados en la API del monedero de Lumen viven bajo
`lumen/interfaces/dtos/v1/`: un fichero por DTO. El nombre del directorio codifica
una convención que vale la pena señalar: `interfaces` contiene los contratos que
ve el mundo exterior, y `v1` los versiona para que una futura forma de carga útil
`v2` pueda convivir junto a la antigua sin romper a los clientes existentes. Aquí
los tienes completos.

::: listing lumen/interfaces/dtos/v1/open_wallet_request.py | Listado 4.2a — OpenWalletRequest: carga útil de apertura de monedero
from __future__ import annotations

from pydantic import BaseModel, Field

from lumen.interfaces.enums.v1.currency import Currency


class OpenWalletRequest(BaseModel):
    """Wallet-opening request payload."""

    owner_id: str = Field(
        min_length=1,
        max_length=64,
        description="Identifier of the wallet owner",
    )
    currency: Currency = Field(
        default=Currency.EUR,
        description="ISO-4217 currency the wallet holds",
    )
:::

::: listing lumen/interfaces/dtos/v1/deposit_request.py | Listado 4.2b — DepositRequest: carga útil de depósito/retirada
from __future__ import annotations

from pydantic import BaseModel, Field


class DepositRequest(BaseModel):
    """Deposit/withdrawal request payload.

    Shared by POST /{id}/deposit and POST /{id}/withdraw — both move a
    positive amount of money in the wallet's own currency.
    """

    amount: int = Field(
        gt=0,
        description="Amount in minor units (cents); must be positive",
    )
:::

::: listing lumen/interfaces/dtos/v1/wallet_dto.py | Listado 4.2c — WalletDto: respuesta completa del monedero
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency


class WalletDto(BaseModel):
    """Full wallet representation returned to clients.

    ``balance_minor`` is in minor units (cents); ``balance`` is the same
    value rendered as a major-unit decimal for human-friendly display.
    """

    id: str
    owner_id: str
    currency: Currency
    balance_minor: int
    balance: float
    created_at: datetime
:::

::: listing lumen/interfaces/dtos/v1/balance_dto.py | Listado 4.2d — BalanceDto: proyección ligera del saldo
from __future__ import annotations

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency


class BalanceDto(BaseModel):
    """Lightweight balance projection for the balance endpoint."""

    id: str
    currency: Currency
    balance_minor: int
    balance: float
:::

Estos son modelos Pydantic puros: PyFly no les añade nada. Vale la pena
desglosar cuatro decisiones de diseño.

`OpenWalletRequest.owner_id` usa `Field(min_length=1, max_length=64)`. El límite
inferior evita monederos fantasma a partir de IDs de propietario en cadena vacía
que contaminarían silenciosamente tus datos; el límite superior mantiene los
identificadores dentro de un ancho de columna razonable cuando llegue la capa de
base de datos en el Capítulo 5.

`currency` es un enum `Currency` (un `StrEnum` con `EUR`, `USD`, `GBP`). Usar un
enum en lugar de una cadena en bruto significa que Pydantic rechaza `"XYZ"` en el
momento de la deserialización: nunca validas el código de divisa tú mismo.
`Field(default=Currency.EUR)` proporciona un valor por defecto razonable para que
los llamantes puedan omitir el campo en monederos en EUR.

`DepositRequest.amount` es un `int` con `Field(gt=0)`. Almacenar el dinero en
unidades menores evita el redondeo de coma flotante: `1050` significa 10,50 € en
un monedero en EUR. La restricción `gt=0` convierte un depósito cero o negativo
en un error de cliente 422, no en una decisión de lógica de negocio: la
restricción vive en el tipo, y Pydantic la aplica antes de que se ejecute tu
manejador.

`WalletDto` y `BalanceDto` son modelos de respuesta. Devolver un modelo Pydantic
tipado en lugar de un `dict` corriente permite al framework generar esquemas de
respuesta OpenAPI precisos y da a los clientes un contrato legible por máquina.

### Usar Valid[T] en un manejador

Envuelve `Body[T]` en `Valid` para optar por errores 422 estructurados cuando
falle la validación:

```python
@post_mapping("", status_code=201)
async def open_wallet(
    self, request: Valid[Body[OpenWalletRequest]]
) -> dict[str, str]:
    ...

@post_mapping("/{wallet_id}/deposit")
async def deposit(
    self,
    wallet_id: PathVar[str],
    request: Valid[Body[DepositRequest]],
) -> dict[str, int | str]:
    ...
```

`Valid[Body[OpenWalletRequest]]` le dice dos cosas al resolver: vincula desde el
cuerpo de la petición (`Body`) y ejecuta la validación de Pydantic antes de que
se ejecute el manejador (`Valid`). Cuando el cuerpo falla la validación, el
resolver captura el `ValidationError` y lanza una `ValidationException` con
`code="VALIDATION_ERROR"` y un array `context.errors` que contiene cada detalle a
nivel de campo.

### Lo que ve el cliente en caso de fallo

Para ver la validación en acción, envía un `POST /api/v1/wallets` con un
`owner_id` vacío:

```
POST /api/v1/wallets
Content-Type: application/json

{"owner_id": ""}
```

!!! tip "Pruébalo"
    Con el servidor en marcha, envía la carga útil incorrecta y observa el `422`:

    ```bash
    curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8080/api/v1/wallets \
      -H 'Content-Type: application/json' \
      -d '{"owner_id": ""}'
    ```

    La opción `-w '\nHTTP %{http_code}\n'` imprime la línea de estado después del
    cuerpo, para que puedas confirmar que es `HTTP 422`: no el `201` que devuelve
    una petición válida, ni un `500`. El cuerpo es el sobre estructurado que se
    muestra a continuación. Prueba una segunda variante —
    `-d '{"owner_id": "alice", "currency": "XYZ"}'`— para ver cómo el enum
    `Currency` rechaza un código desconocido con la misma forma de sobre.

La respuesta es HTTP 422:

```json
{
  "error": {
    "message": "Validation failed: owner_id: String should have at least 1 character",
    "code": "VALIDATION_ERROR",
    "status": 422,
    "path": "/api/v1/wallets",
    "timestamp": "2026-06-07T10:30:00+00:00",
    "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "context": {
      "errors": [
        {
          "type": "string_too_short",
          "loc": ["owner_id"],
          "msg": "String should have at least 1 character",
          "input": "",
          "ctx": {"min_length": 1}
        }
      ]
    }
  }
}
```

Cada error de campo lleva un `type` (legible por máquina), un `loc` (ruta hasta el
campo que falla), un `msg` (legible por humanos) y un `input` (el valor
rechazado). Los consumidores de la API parsean este array de forma determinista:
sin rascar cadenas de error.

La diferencia entre `Body[T]` a secas y `Valid[Body[T]]` es exactamente esta:

| Anotación | Cuando falla la validación |
|---|---|
| `Body[T]` | Se propaga el `ValidationError` de Pydantic en bruto: puede convertirse en un 500 sin gestión adicional |
| `Valid[Body[T]]` | Capturado, convertido en `ValidationException`, produce siempre un 422 estructurado |

Usa `Valid[Body[T]]` en cada endpoint que acepte entrada del usuario.

!!! spring "Equivalencia con Spring"
    `Valid[Body[T]]` mapea directamente a la combinación de `@Valid` +
    `@RequestBody` de Spring en un método `@RestController`. En Spring escribes
    `@PostMapping public ResponseEntity create(@Valid @RequestBody OpenWalletRequest body)`;
    en PyFly escribes
    `async def open_wallet(self, request: Valid[Body[OpenWalletRequest]])`. La
    forma de la respuesta 422 (errores a nivel de campo con rutas de ubicación)
    refleja la carga útil de `MethodArgumentNotValidException` de Spring Boot 3.

**Qué acaba de pasar.** Las reglas de validación nunca salieron del DTO.
`Field(min_length=1)` en `owner_id`, el enum `Currency` y `Field(gt=0)` en
`amount` son toda la especificación, y envolver el cuerpo en `Valid` convirtió
cualquier infracción de esas reglas en un `422` predecible y legible por máquina
antes de que se ejecutara tu manejador. Escribiste las restricciones una vez,
sobre los datos; el framework las aplicó en todas partes donde llegan los datos.

---

## Errores en los que los clientes pueden confiar

Una API bien diseñada falla de forma ruidosa, consistente e informativa. Los
clientes nunca deben parsear trazas de pila de excepciones ni adivinar qué fue
mal a partir de un 500 genérico. El reto es lograr esto sin dispersar lógica
específica de HTTP por todo tu código de servicio: el código de estado HTTP es
una preocupación de infraestructura, no de negocio.

La jerarquía de excepciones de PyFly es la columna vertebral de su historia de
errores. Cada excepción del árbol lleva tres cosas: un `message` legible por
humanos, un `code` legible por máquina y un dict `context` opcional para detalle
de depuración. El manejador global de excepciones de la capa web mapea cada
subclase al código de estado HTTP correcto automáticamente: tú haces `raise`, el
framework responde.

### El árbol de excepciones

```
PyFlyException
├── BusinessException          → 400 (catch-all)
│   ├── ValidationException    → 422
│   ├── ResourceNotFoundException → 404
│   ├── ConflictException      → 409
│   ├── InvalidRequestException → 400
│   └── ...
├── SecurityException          → 403
│   ├── UnauthorizedException  → 401
│   └── ForbiddenException     → 403
└── InfrastructureException    → 502 (catch-all)
    ├── ServiceUnavailableException → 503
    ├── CircuitBreakerException → 503
    └── ...
```

La jerarquía es deliberadamente plana. `BusinessException` cubre cualquier cosa
que sea culpa del llamante; `InfrastructureException` cubre cualquier cosa que sea
culpa del sistema. Las subclases fijan el código de estado. Cuando un nuevo error
de dominio no encaja en una subclase existente, extiende el padre más cercano y el
código de estado viene gratis.

Impórtalas de `pyfly.kernel`:

```python
from pyfly.kernel import (
    ResourceNotFoundException,
    ConflictException,
    ValidationException,
    InvalidRequestException,
)
```

Lánzalas desde el código del manejador sin preocuparte por HTTP:

```python
raise ResourceNotFoundException(
    f"Wallet {wallet_id!r} not found",
    code="WALLET_NOT_FOUND",
    context={"wallet_id": wallet_id},
)
```

El manejador global la captura, la mapea a 404 y emite una respuesta JSON
estructurada:

```json
{
  "error": {
    "message": "Wallet 'w-999' not found",
    "code": "WALLET_NOT_FOUND",
    "status": 404,
    "path": "/api/v1/wallets/w-999",
    "timestamp": "2026-06-07T10:30:00+00:00",
    "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "context": {
      "wallet_id": "w-999"
    }
  }
}
```

!!! tip "Pruébalo"
    Pide un monedero que nunca se abrió y observa cómo el framework convierte tu
    `raise` en un `404` limpio:

    ```bash
    curl -s -w '\nHTTP %{http_code}\n' localhost:8080/api/v1/wallets/w-999
    ```

    La línea de estado dice `HTTP 404` y el cuerpo es el sobre de arriba, con
    `"code": "WALLET_NOT_FOUND"` y tu `context` trasladado al pie de la letra.
    Nunca escribiste un código de estado en `get_wallet`:
    `ResourceNotFoundException` se mapea a 404 por ti. Fíjate en el
    `transaction_id` de la respuesta; cópialo y haz grep en el log de tu servidor
    para encontrar la petición exacta.

El `transaction_id` es gratis: el `TransactionIdFilter` asigna un UUID a cada
petición y lo enhebra por todas las respuestas de error. Los clientes lo
registran; el soporte lo usa para encontrar la entrada de log de servidor
correspondiente. Un solo ID es todo lo que se necesita para reconstruir lo que
pasó.

**Qué acaba de pasar.** Tu manejador expresó un hecho de dominio —«este monedero
no existe»— lanzando una excepción tipada con un mensaje, un código y algo de
contexto. El manejador global de la capa web hizo la traducción a HTTP: eligió el
código de estado a partir de la clase de la excepción, lo envolvió todo en el
sobre de error estándar y estampó un `transaction_id`. Las preocupaciones de HTTP
se mantuvieron completamente fuera de tu código de negocio.

!!! note "RFC 7807"
    El sobre de error por defecto —`{"error": {...}}`— es el formato propio de
    PyFly. Si tu equipo prefiere el estándar del IETF, fija
    `pyfly.web.problem-details.enabled: true` en `pyfly.yaml`. Con esa opción
    activada, la misma `ResourceNotFoundException` produce una respuesta
    `application/problem+json` con `type`, `title`, `status`, `detail` e
    `instance` como los miembros estándar de RFC 7807, más `code` y
    `transactionId` como miembros de extensión de PyFly. Ambos modos usan la
    misma jerarquía de excepciones y el mismo mapeo de estados.

---

## Negociación de contenido y OpenAPI

### JSON y XML

Devolver un `dict` o un modelo Pydantic no es del todo el final de la historia.
En algún punto entre la sentencia `return` de tu manejador y los bytes que recibe
el cliente, el framework decide un formato de cable. En lugar de codificar JSON a
fuego, PyFly pasa el valor de retorno por una cadena ordenada de
`HttpMessageConverter`: importante para APIs empresariales que deben servir a
socios de XML o negociar el formato más ligero para clientes móviles.

JSON es el predeterminado. Cuando no se envía ninguna cabecera `Accept`, la
respuesta es `application/json`. Cuando el cliente envía
`Accept: application/xml`, el conversor de XML toma el relevo y serializa el mismo
valor de retorno como XML, sin cambio alguno en el código de tu manejador:

```
GET /api/v1/wallets/w-001   Accept: application/json
  →  {"id": "w-001", ...}

GET /api/v1/wallets/w-001   Accept: application/xml
  →  <response><id>w-001</id>...</response>
```

La misma negociación se aplica a los datos entrantes: un parámetro `Body[T]` o
`Valid[Body[T]]` acepta cuerpos de petición tanto con
`Content-Type: application/json` como con `Content-Type: application/xml`. JSON es
el recurso de reserva cuando no hay ninguna cabecera `Content-Type` presente.

### Documentación autogenerada

Las especificaciones de API mantenidas manualmente se desvían. A medida que
cambian las rutas, se renombran los parámetros y se añaden nuevos modelos, las
especificaciones escritas a mano se quedan atrás respecto al código. PyFly elimina
esto por completo generando la documentación a partir de los mismos metadatos que
impulsan el enrutamiento: la especificación está siempre sincronizada porque es la
misma fuente.

Tan pronto como Lumen arranca, tres endpoints de documentación quedan activos sin
coste alguno:

| Endpoint | Propósito |
|---|---|
| `/docs` | Swagger UI: documentación interactiva, pruébalo-ya |
| `/redoc` | ReDoc: documentación de referencia limpia, de dos paneles |
| `/openapi.json` | Especificación OpenAPI 3.0 en bruto |

El `OpenAPIGenerator` introspecciona los metadatos de ruta de
`ControllerRegistrar` —cada ruta, método, variable de ruta, parámetro de consulta
y esquema de petición/respuesta (a partir de la introspección de modelos
Pydantic)— y ensambla la especificación en el arranque. Nunca escribes la
especificación a mano. Los endpoints de documentación viven en el puerto de la
**aplicación** (8080) junto a tu API; están activados por defecto
(`pyfly.web.docs.enabled: true`). Desactívalos en producción con
`pyfly.web.docs.enabled: false` en `pyfly.yaml`.

!!! note "Nota"
    No confundas los endpoints de documentación con el **panel de
    administración**. `/docs`, `/redoc` y `/openapi.json` describen *tu* API y se
    sirven en el puerto de la app (8080). El Panel de Administración de PyFly
    (`/admin`) y los endpoints de salud del actuator (`/actuator/*`) describen el
    *proceso en ejecución* y se sirven en el puerto de **gestión** separado
    (`pyfly.management.server.port`, por defecto 9090), introducido en el
    Capítulo 3. Son dos listeners distintos con dos audiencias distintas.

!!! tip "Pruébalo"
    Con el servidor en marcha, obtén la especificación en bruto y confirma que
    tus rutas están en ella:

    ```bash
    curl -s localhost:8080/openapi.json | head -c 200
    ```

    Verás la cabecera de OpenAPI 3.0 y el comienzo del mapa `paths`. Luego abre
    `http://localhost:8080/docs` en un navegador. Verás
    `POST /api/v1/wallets`, `GET /api/v1/wallets/{wallet_id}`,
    `POST /api/v1/wallets/{wallet_id}/deposit` y los demás: cada uno con los
    esquemas de petición y respuesta correctos derivados de tus modelos Pydantic,
    y el parámetro de consulta `owner_id` de `list_wallets` ya documentado con su
    tipo y valor por defecto. Haz clic en «Try it out» en `POST /api/v1/wallets`
    para abrir un monedero real directamente desde el navegador.

**Qué acaba de pasar.** No escribiste ni una línea de documentación de API y, sin
embargo, apareció una especificación completa, interactiva y siempre exacta. Los
mismos metadatos de ruta y modelo que impulsan la gestión de peticiones impulsan
también la documentación, de modo que las dos nunca pueden desviarse una de otra.

---

## El servidor por debajo

Lumen ya tiene rutas, vinculaciones, validación y documentación. La última
pregunta es qué escucha realmente en el puerto 8080. La respuesta importa:
distintos servidores hacen distintos compromisos en rendimiento, soporte de
versión HTTP, compatibilidad con el SO y herramientas del ecosistema. Atar una
aplicación a un único servidor a nivel de framework te obliga a aceptar esos
compromisos de forma permanente.

Un **servidor ASGI** es el proceso que realmente acepta conexiones TCP, parsea
HTTP y llama a tu aplicación: la capa entre el socket del sistema operativo y tus
manejadores. PyFly no codifica uno a fuego. En el arranque,
`ServerAutoConfiguration` ejecuta una selección en cascada basada en lo que está
instalado:

| Prioridad | Servidor | Característica |
|---|---|---|
| 1.ª | **Granian** | Impulsado por Rust/tokio; el mayor rendimiento con un solo worker |
| 2.ª | **Uvicorn** | Estándar del ecosistema; el mejor soporte de herramientas |
| 3.ª | **Hypercorn** | HTTP/2 y HTTP/3 nativos |

Los tres arrancan a través del mismo protocolo `ApplicationServerPort`, de modo
que tu código es completamente ajeno a cuál se está ejecutando. Sobrescríbelo con
`pyfly.server.type: uvicorn` en `pyfly.yaml` o con la opción de CLI `--server`:

```bash
pyfly run --server uvicorn --reload      # development: auto-reload
pyfly run --server granian --workers 4  # production: multi-worker
```

!!! tip "Pruébalo"
    Para el desarrollo del día a día, ejecuta con auto-reload para que el
    servidor se reinicie en cada guardado:

    ```bash
    uv run pyfly run --reload
    ```

    PyFly registra el servidor elegido y el puerto enlazado en el arranque. Como
    `--reload` requiere un vigilante de ficheros integrado, PyFly selecciona
    **Uvicorn** para el modo reload independientemente del orden de la cascada.
    Edita un manejador, guarda y observa cómo el log informa del reinicio; luego
    vuelve a ejecutar cualquier `curl` de antes y ve tu cambio en vivo sin
    detener el proceso.

El bucle de eventos también es enchufable: `uvloop` (Linux/macOS) y `winloop`
(Windows) se seleccionan automáticamente cuando están instalados, entregando una
mejora de rendimiento de 2 a 4× sobre el asyncio por defecto. Instálalos con
`uv add "pyfly[web-fast]"`.

!!! tip "Consejo"
    Para desarrollo, `pyfly run --reload` es todo lo que necesitas: elige el
    mejor servidor y bucle de eventos disponibles automáticamente. Para
    producción, pasa un recuento de workers positivo y explícito para escalar a
    través de los núcleos: `pyfly run --server granian --workers 4`, como en el
    ejemplo de arriba. Un valor de `--workers` igual a `0` o negativo se resuelve
    a un solo worker, de modo que el multi-worker es siempre una opción explícita.
    Las opciones de la CLI siempre sobrescriben `pyfly.yaml`.

---

## Lo que construiste {.recap}

La Parte I está completa.

En cuatro capítulos pasaste de un andamiaje vacío a un servicio con forma de
producción. Lumen ahora **arranca** (`@pyfly_application`, banner de arranque,
logging estructurado), está **cableado** (servicios y repositorios conectados
mediante inyección por constructor sin código de pegamento), **configurado**
(`pyfly.yaml` de cuatro capas + superposiciones de perfil + secretos por variable
de entorno, `WalletProperties` tipado) y **sirve**: una API REST validada en
`/api/v1/wallets` con vinculación de cuerpo `PathVar`, `QueryParam` y
`Valid[Body[T]]`; errores 422 estructurados a partir de restricciones de
Pydantic; mapeo de error-de-dominio-a-estado desde la jerarquía de excepciones;
modelos de respuesta tipados (`WalletDto`, `BalanceDto`) que impulsan la
generación de esquemas OpenAPI; y un servidor ASGI enchufable corriendo por
debajo.

Cada parte de esta pila sigue el mismo principio hexagonal que has visto a lo
largo del libro: tu código depende de puertos y decoradores; el framework cablea
los adaptadores. Cambia el almacén en memoria por un adaptador de PostgreSQL en el
Capítulo 5, sustituye el despacho directo por un bus CQRS completo en el Capítulo
7, o habilita respuestas XML: nada de eso requiere tocar la estructura de
decoradores del controlador ni las formas de los DTO.

La Parte II lleva a Lumen más lejos: datos persistentes con SQLAlchemy, eventos de
dominio, resiliencia con cortacircuitos (circuit breakers) y seguridad con JWT.
Los cimientos que construiste aquí se llevan adelante intactos.

---

## Pruébalo tú mismo {.exercises}

Cada ejercicio es pequeño y autocontenido. Después de cada cambio, reinicia con
`uv run pyfly run --reload` y vuelve a ejecutar el `curl` sugerido para confirmar
el comportamiento. Si tienes instaladas las dependencias de desarrollo, también
puedes ejecutar la suite de pruebas del proyecto en cualquier momento para
asegurarte de que nada ha regresionado:

```bash
uv run --extra dev pytest
```

Deberías ver una fila de puntos que pasan y una línea de resumen `passed`.

1. **Añade un endpoint `DELETE /api/v1/wallets/{wallet_id}`.** Elimina el
   monedero de `_wallets` y devuelve 204 No Content. Lanza
   `ResourceNotFoundException` si el monedero no existe. Decóralo con
   `@delete_mapping("/{wallet_id}", status_code=204)`: PyFly convierte un retorno
   `None` con `status_code=204` en una respuesta 204 sin cuerpo. Verifícalo con
   `curl -X DELETE http://localhost:8080/api/v1/wallets/{id}`.

2. **Añade filtrado por divisa a `list_wallets`.** Añade un parámetro
   `currency: QueryParam[str] = None` y filtra `_wallets.values()` cuando no sea
   `None`. Pruébalo con `GET /api/v1/wallets?currency=EUR` y confirma que solo se
   devuelven los monederos en EUR; confirma que `GET /api/v1/wallets` sin el
   parámetro devuelve todos los monederos. Luego haz que el parámetro sea
   obligatorio eliminando el valor por defecto: observa la respuesta 400 cuando lo
   omites de la petición.

3. **Lanza una subclase específica de dominio de `ResourceNotFoundException`.**
   Crea un `WalletNotFoundError` que herede de `ResourceNotFoundException` y lleve
   un campo `currency` extra en `context`. Lánzalo desde `get_wallet` y
   `get_balance` en lugar de `ResourceNotFoundException` a secas. Verifica que la
   respuesta de error JSON incluye el campo de contexto extra sin ningún cambio en
   el manejador global: la jerarquía lo mapea a 404 automáticamente.
