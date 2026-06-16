<span class="eyebrow">Capítulo 16</span>

# Pruebas de aplicaciones PyFly {.chtitle}

::: figure art/openers/ch16.svg | &nbsp;

El monedero (wallet) funciona. Los depósitos entran, los saldos se actualizan, los eventos se propagan por el bus y el coordinador de la saga revierte limpiamente ante un fallo. Lo que todavía no tienes es la **confianza** de que seguirá funcionando tras la próxima refactorización. Esa confianza viene de las pruebas: pruebas que se ejecutan en milisegundos, que demuestran que el modelo de dominio hace cumplir sus invariantes, que verifican que la canalización CQRS despacha correctamente, que ejercitan las consultas derivadas y los predicados de tipo Specification del repositorio contra una base de datos SQLite real, y que arrancan el contexto de aplicación completo en una prueba de integración que demuestra la composición entera de inyección de dependencias + persistencia.

PyFly trata las pruebas como un asunto de primer orden. El módulo `pyfly.testing` incluye ayudantes de más alto nivel —`PyFlyTestCase`, `create_test_container`, `assert_event_published`, el cableado de Testcontainers— a los que puedes recurrir cuando los necesites. La propia suite de pruebas de Lumen no los usa: cablea componentes reales directamente desde `conftest.py`, emplea fixtures estándar de pytest y cubre cada nivel de la pirámide sin código repetitivo. Ese es el enfoque que enseña este capítulo.

::: figure art/figures/16-testing.svg | Figura 16.1 — La pirámide de pruebas de PyFly. Las pruebas unitarias rápidas forman la base ancha; las pruebas de integración ocupan el centro; las pruebas de adaptador contra una BD real y una prueba de integración con el contexto arrancado coronan la cima.

La pirámide tiene cuatro niveles. Las **pruebas unitarias** están en la base —muchas de ellas, ejecutándose en milisegundos, ejercitando el modelo de dominio sin dependencias—. Las **pruebas de flujo CQRS** ocupan el siguiente escalón: el ciclo completo de apertura/depósito/retiro/consulta enrutado a través del bus real y del repositorio real, todo cableado en `conftest.py`. Las **pruebas de repositorio** ejercitan las consultas derivadas, la paginación y los predicados de tipo Specification contra SQLite. En la cúspide, una **prueba de integración con el contexto arrancado** inicia el `ApplicationContext` real —escaneo de inyección de dependencias, autoconfiguración de CQRS, `RepositoryBeanPostProcessor`, la costura `@transactional`, la arquitectura orientada a eventos (EDA)— y recorre el ciclo de vida completo.

| Nivel                   | Dependencias                          | Velocidad | Enfoque de Lumen                   |
|-------------------------|---------------------------------------|-----------|------------------------------------|
| Unitario                | Ninguna                               | Rápido    | pytest puro, sin fixtures          |
| Flujo CQRS              | Bus real + repositorio sobre SQLite   | Rápido    | fixtures de conftest.py            |
| Repositorio             | SQLite + aiosqlite                    | Rápido    | `tmp_path` + SQLAlchemy            |
| Contexto arrancado      | ApplicationContext completo + SQLite  | Rápido    | sustitución de entorno `monkeypatch` |

El proyecto usa pytest con `pytest-asyncio` en **modo automático**. Actívalo una vez en `pyproject.toml` y cada función `async def test_*` se recopila automáticamente:

```ini
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
```

Una glosa rápida antes de empezar, ya que tres términos se repiten en este capítulo. Un
**fixture** es una pieza reutilizable de preparación de prueba: pytest la construye una vez, se la
entrega a tu prueba y la desmonta después. Un **conftest.py** es un archivo especial que pytest
descubre automáticamente; cualquier fixture que declares ahí pasa a estar disponible para cada
prueba del paquete sin necesidad de importarla. Y el **modo automático de `pytest-asyncio`** simplemente
significa que puedes escribir `async def test_...` y pytest hará el `await` por ti: no hace falta
un decorador por prueba. Cada uno de estos vuelve a aparecer con un ejemplo concreto
más adelante; esto es solo el mapa.

Instala las dependencias de desarrollo y ejecuta la suite:

```bash
uv run --extra dev pytest -q
```

El `uv sync` a secas (sin `--extra dev`) omite el grupo de desarrollo, así que pytest no queda
instalado. Incluye siempre `--extra dev` al ejecutar las pruebas.

**Ejecútalo.** Desde el directorio `samples/lumen`, ejecuta toda la suite una vez ahora para que tengas
una referencia de base antes de cambiar nada:

```bash
uv run --extra dev pytest -q
```

Deberías ver una hilera de puntos —uno por prueba— seguida de una línea de resumen:

```text
.........................................                                [100%]
41 passed in 0.28s
```

Cuarenta y una pruebas superadas, en menos de un tercio de segundo, sin Docker ni proceso
externo alguno. Esa velocidad es el sentido entero de la pirámide: la base rápida atrapa la mayoría
de las regresiones antes de que lleguen a ejecutarse las capas de integración más lentas. Si en cambio ves
`No module named pytest`, es que olvidaste `--extra dev`; vuelve a ejecutarlo con esa opción.

---

## Pruebas unitarias del dominio

El modelo de dominio —`Money` y `Wallet`— no tiene dependencias del framework. Nunca toca una base de datos, un bus de mensajes ni un cliente HTTP. Esa pureza lo convierte en el objetivo ideal para pruebas unitarias rápidas: construye objetos, llama a métodos, comprueba resultados. Sin mocks, sin fixtures, sin `async`.

### Pruebas de Money

`Money` es una dataclass congelada (frozen). Cada operación o bien tiene éxito y devuelve una nueva instancia de `Money`, o bien lanza `BusinessRuleViolation`. Cada violación lleva una cadena `.rule` que nombra el invariante incumplido, útil para verificar la regla exacta en las pruebas.

Una glosa rápida sobre dos términos. Un **objeto de valor** es un objeto definido por completo por sus
valores: dos instancias de `Money(1050, EUR)` son iguales porque sus campos son iguales,
no porque sean el mismo objeto en memoria. Una **dataclass congelada (frozen)** es la forma que tiene Python
de hacer inmutable ese objeto: una vez construido, no puedes reasignar sus
campos. Juntos hacen que `Money` sea seguro de pasar libremente: ningún consumidor puede
mutarlo a tus espaldas, así que nunca necesita copia defensiva.

Construyamos el archivo de pruebas un grupo de aserciones a la vez. Cada paso de abajo se corresponde con
una función `def test_...` del listado que sigue.

**Paso 1 — la igualdad es estructural.** `test_value_equality_is_structural` comprueba
que dos valores `Money` con el mismo importe y la misma moneda son iguales, y que
diferir en cualquiera de los dos campos los hace distintos. Este es el contrato del objeto de valor.

**Paso 2 — la inmutabilidad se hace cumplir.** `test_money_is_immutable` intenta asignar a
`money.amount` y espera una excepción. La dataclass congelada lanza
`FrozenInstanceError`, demostrando que no puedes mutar un valor tras su construcción.

**Paso 3 — la aritmética devuelve valores nuevos.** `test_add_and_subtract_same_currency`
comprueba que `add` y `subtract` producen el `Money` esperado, sin mutar nunca los
operandos.

**Paso 4 — la superficie de conveniencia.** `test_zero_factory_and_major_units` cubre la
fábrica `Money.zero(currency)`, la propiedad `major_units` y el formateo de
`__str__`.

**Paso 5 — los invariantes rechazan la entrada inválida.** Las dos últimas pruebas comprueban que mezclar
monedas y pasar un importe no entero lanzan cada una `BusinessRuleViolation` con
una cadena `.rule` específica: `"money-currency-mismatch"` y
`"money-amount-integer"`.

::: listing tests/test_money.py | Listado 16.1 — Pruebas unitarias puras del objeto de valor Money
from __future__ import annotations

import pytest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money

from pyfly.domain import BusinessRuleViolation


def test_value_equality_is_structural() -> None:
    assert Money(1050, Currency.EUR) == Money(1050, Currency.EUR)
    assert Money(1050, Currency.EUR) != Money(1050, Currency.USD)
    assert Money(1050, Currency.EUR) != Money(999, Currency.EUR)


def test_money_is_immutable() -> None:
    money = Money(1050, Currency.EUR)
    with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
        money.amount = 0  # type: ignore[misc]


def test_add_and_subtract_same_currency() -> None:
    a = Money(1050, Currency.EUR)
    b = Money(450, Currency.EUR)
    assert a.add(b) == Money(1500, Currency.EUR)
    assert a.subtract(b) == Money(600, Currency.EUR)


def test_zero_factory_and_major_units() -> None:
    assert Money.zero(Currency.USD) == Money(0, Currency.USD)
    assert Money(1050, Currency.EUR).major_units == 10.5
    assert str(Money(1050, Currency.EUR)) == "10.50 EUR"


def test_currency_mismatch_is_rejected() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Money(100, Currency.EUR).add(Money(100, Currency.USD))
    assert exc.value.rule == "money-currency-mismatch"


def test_non_integer_amount_is_rejected() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Money(10.5, Currency.EUR)  # type: ignore[arg-type]
    assert exc.value.rule == "money-amount-integer"
:::

Cada prueba es síncrona: sin `async`, sin `await`, sin fixtures. Pytest recopila las funciones a nivel de módulo automáticamente. `Currency.EUR` es un valor de enumeración, no una cadena simple, ajustándose exactamente al contrato de tipos del modelo de dominio.

**Ejecútalo.** Ejecuta solo este archivo para ver en acción la base unitaria de la pirámide:

```bash
uv run --extra dev pytest tests/test_money.py -q
```

Salida esperada:

```text
......                                                                   [100%]
6 passed in 0.02s
```

Seis pruebas, veinte milisegundos. Sin base de datos conectada, sin bus de eventos iniciado: estas
pruebas construyen un objeto simple y comprueban su comportamiento. Eso es lo que hace que la
base de la pirámide sea tan ancha y tan rápida.

*Qué acaba de pasar.* Has demostrado todo el contrato de `Money` —igualdad,
inmutabilidad, aritmética y cada invariante— sin tocar una sola pieza de
infraestructura del framework. El código de dominio puro permanece probable como dominio puro. Cuando una de
estas falla, sabes que el error está en el propio objeto de valor, no en el cableado, una
sesión o el bus.

!!! tip "Aritmética de unidades menores"
    `Money` almacena los importes en **unidades menores** (céntimos enteros). `Money(1050,
    Currency.EUR)` representa 10,50 € —verificado por `major_units == 10.5` y
    `str(...) == "10.50 EUR"`—. La fábrica `Money.zero(currency)` devuelve un
    `Money(0, currency)`, útil para inicializar los saldos de los monederos.

### Pruebas de la raíz de agregado Wallet

`Wallet` hace cumplir varios invariantes: el propietario debe ser una cadena no vacía, los depósitos deben ser positivos, los retiros no deben dejar el saldo en descubierto y los importes deben coincidir con la moneda del monedero. Cada violación lleva un atributo `.rule` para una verificación precisa.

Un término primero. Un **agregado** (o **raíz de agregado**) es un grupo de objetos de
dominio tratado como una sola unidad de coherencia; aquí, el `Wallet` y su
saldo. Todo cambio pasa por los métodos del agregado, así que el agregado es el
único lugar que garantiza que sus invariantes se cumplen. Eso lo convierte en la unidad natural para
probar: recórrelo a través de sus métodos públicos y comprueba que las reglas nunca se rompen.

El patrón que sigue cada prueba de agregado es **organizar, actuar, comprobar** (arrange, act, assert). Organizar:
construir el monedero en un estado conocido. Actuar: llamar a un método. Comprobar: verificar el
saldo, el evento emitido o la violación lanzada. Búscalo en cada prueba:

**Paso 1 — la apertura emite un evento.** `test_open_creates_empty_wallet` abre un monedero
y comprueba que el saldo es cero y que se encola exactamente un evento `WalletOpened`.

**Paso 2 — la apertura valida sus argumentos.** `test_open_requires_owner` pasa un
propietario en blanco y espera `BusinessRuleViolation` con la regla
`"wallet-owner-required"`.

**Paso 3 — el camino feliz de depósito y luego retiro.**
`test_deposit_then_withdraw_happy_path` deposita, comprueba el saldo y el
evento `FundsDeposited`, luego retira y comprueba el saldo y el
evento `FundsWithdrawn`. Fíjate en la llamada a `clear_events()` en el paso de organización; más sobre esto
justo debajo del listado.

**Paso 4 — los invariantes rechazan las operaciones inválidas.** Las tres pruebas finales demuestran que un
retiro no puede dejar el saldo en descubierto, que un depósito debe ser positivo y que un importe debe coincidir con la
moneda del monedero. Cada una comprueba la cadena `.rule` exacta, y la prueba de descubierto también
comprueba que el saldo quedó sin cambios y que no se lanzó ningún evento, prueba de que el invariante
se disparó *antes* de que cambiara ningún estado.

::: listing tests/test_wallet_aggregate.py | Listado 16.2 — Pruebas unitarias de la raíz de agregado Wallet
from __future__ import annotations

import pytest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import (
    FundsDeposited,
    FundsWithdrawn,
    Wallet,
    WalletOpened,
)

from pyfly.domain import BusinessRuleViolation


def test_open_creates_empty_wallet() -> None:
    wallet = Wallet.open("wlt-1", "owner-1", Currency.EUR)
    assert wallet.owner_id == "owner-1"
    assert wallet.currency is Currency.EUR
    assert wallet.balance == Money.zero(Currency.EUR)
    [event] = wallet.pending_events()
    assert isinstance(event, WalletOpened)
    assert event.wallet_id == "wlt-1"
    assert event.currency == "EUR"


def test_open_requires_owner() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Wallet.open("wlt-x", "   ", Currency.EUR)
    assert exc.value.rule == "wallet-owner-required"


def test_deposit_then_withdraw_happy_path() -> None:
    wallet = Wallet.open("wlt-2", "owner-2", Currency.EUR)
    wallet.clear_events()

    wallet.deposit(Money(1000, Currency.EUR))
    assert wallet.balance == Money(1000, Currency.EUR)
    [event] = wallet.clear_events()
    assert isinstance(event, FundsDeposited)
    assert event.amount == 1000
    assert event.balance == 1000

    wallet.withdraw(Money(400, Currency.EUR))
    assert wallet.balance == Money(600, Currency.EUR)
    [event] = wallet.clear_events()
    assert isinstance(event, FundsWithdrawn)
    assert event.amount == 400
    assert event.balance == 600


def test_withdraw_cannot_overdraw() -> None:
    wallet = Wallet.open("wlt-3", "owner-3", Currency.EUR)
    wallet.deposit(Money(500, Currency.EUR))
    wallet.clear_events()
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.withdraw(Money(501, Currency.EUR))
    assert exc.value.rule == "wallet-insufficient-funds"
    # invariant held: balance unchanged, no event raised
    assert wallet.balance == Money(500, Currency.EUR)
    assert wallet.pending_events() == []


def test_deposit_must_be_positive() -> None:
    wallet = Wallet.open("wlt-4", "owner-4", Currency.EUR)
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.deposit(Money(0, Currency.EUR))
    assert exc.value.rule == "wallet-deposit-positive"


def test_currency_mismatch_is_rejected() -> None:
    wallet = Wallet.open("wlt-5", "owner-5", Currency.EUR)
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.deposit(Money(100, Currency.USD))
    assert exc.value.rule == "wallet-currency-mismatch"
:::

Tres detalles merecen atención. Primero, `Wallet.open` toma tres argumentos posicionales: un `wallet_id` pregenerado, un `owner_id` y un valor de enumeración `Currency`; el agregado no genera su propio ID. Segundo, `pending_events()` devuelve los eventos almacenados en búfer sin vaciarlos; `clear_events()` los devuelve y los vacía. La prueba `test_deposit_then_withdraw_happy_path` llama a `clear_events()` tras la apertura para que cada aserción vea exactamente un evento. Tercero, `FundsDeposited` y `FundsWithdrawn` llevan `amount` (el importe de la operación en unidades menores) y `balance` (el saldo acumulado tras la operación), no `new_balance`. Verifica siempre los campos reales de la dataclass del evento antes de comprobarlos.

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_wallet_aggregate.py -q
```

Salida esperada:

```text
......                                                                   [100%]
6 passed in 0.02s
```

*Qué acaba de pasar.* La línea más difícil de leer es la asignación
`[event] = wallet.clear_events()`. Eso es un desempaquetado de lista: comprueba que la lista devuelta
tiene **exactamente un** elemento y lo enlaza a `event` en un solo paso. Si el
agregado hubiera lanzado cero o dos eventos, el propio desempaquetado lanzaría un
`ValueError` y la prueba fallaría, así que la forma del flujo de eventos se comprueba
gratis. Por eso la prueba del camino feliz llama a `clear_events()` justo después de la apertura:
vacía el evento `WalletOpened` para que el siguiente desempaquetado vea solo el
evento `FundsDeposited` que realmente estás comprobando.

!!! spring "Equivalencia con Spring"
    Probar un agregado DDD de forma aislada es la misma disciplina en cualquier stack. En
    Spring / jMolecules llamarías a los métodos del agregado directamente y
    comprobarías `aggregate.domainEvents()` (proporcionado por `AbstractAggregateRoot`)
    antes de llamar a `afterDomainEventPublication()` para vaciar el búfer. El
    `clear_events()` de PyFly cumple el mismo papel: vaciar, comprobar, seguir.

---

## Cableado del stack de pruebas con conftest.py

Las pruebas de CQRS y de listeners de eventos necesitan infraestructura real: un `WalletRepository` respaldado por SQLite, un bus de eventos, manejadores de comandos y consultas y un bus en marcha. En lugar de recrear esto en cada módulo de prueba, Lumen declara el cableado una sola vez en `tests/conftest.py`. Pytest descubre el archivo automáticamente y pone los fixtures a disposición de cada prueba del paquete.

La diferencia clave respecto a una prueba de adaptador montada a mano es que el fixture `repository` usa el **`WalletRepository` real del framework** —la misma clase al estilo Spring Data que arranca la aplicación— y lo ejecuta a través del **`RepositoryBeanPostProcessor` real**, que compila los esbozos de consulta derivada a partir de los nombres de método al arrancar.

Este archivo es el corazón del capítulo, así que lo leeremos de arriba abajo como una serie
de pasos pequeños y por capas. Cada fixture se construye sobre el anterior. Lo que hay que tener
presente: pytest enlaza los fixtures por **nombre**. Cuando una función de fixture declara un
parámetro, pytest busca un fixture con ese nombre, lo construye y lo inyecta. Así
es como un pequeño grafo de fixtures independientes se compone en un stack de pruebas completo.

**Paso 1 — hacer que la muestra sea importable.** Las primeras líneas añaden el `src/` de la
muestra a `sys.path` para que `import lumen...` se resuelva. El ajuste `pythonpath = ["src"]`
de `pyproject.toml` hace lo mismo para la recopilación de la propia pytest; esta
línea cubre las importaciones directas dentro de `conftest.py` antes de que se apliquen los ajustes de ruta de pytest.

**Paso 2 — `session_factory`: un motor de base de datos.** Este fixture asíncrono crea un
motor SQLite en memoria, ejecuta `Base.metadata.create_all` para construir el esquema y
entrega un `async_sessionmaker`. Una **fábrica de sesiones** es un invocable que reparte
sesiones de base de datos nuevas; la autoconfiguración relacional del framework crea una
exactamente así al arrancar. El único motor compartido mantiene la base de datos en memoria
viva durante toda la prueba: ciérralo y los datos se esfuman.

**Paso 3 — `repository`: el repositorio real al estilo Spring Data.** Construye el
`WalletRepository` del framework, luego llama a
`RepositoryBeanPostProcessor().after_init(repo, "walletRepository")`. Un
**post-procesador** es un gancho que se ejecuta contra un bean recién creado; aquí lee
nombres de método como `find_by_owner_id` y los compila en consultas reales. Si te saltas esta
llamada, esos métodos siguen siendo esbozos que lanzan `NotImplementedError`.

**Paso 4 — `event_bus`: el bus de eventos en memoria.** Un fixture de una línea que entrega un
`InMemoryEventBus`, el mismo publicador que usa la aplicación en despliegues sin Kafka.

**Paso 5 — `audit_listener`: un suscriptor en ese bus.** Crea el
`WalletAuditListener` y suscribe su manejador al bus, leyendo los patrones de
evento directamente del atributo `__pyfly_event_patterns__` del método decorado:
exactamente lo que hace el `ApplicationContext` cuando autocablea los listeners al arrancar.

**Paso 6 — `command_bus` y `query_bus`: los despachadores de CQRS.** Cada uno construye un
`HandlerRegistry`, registra los manejadores reales (pasándoles el `repository`,
el `event_bus` y la `session_factory` que necesitan) y entrega un bus. **CQRS**
—Command/Query Responsibility Segregation— significa simplemente que las escrituras van por un bus de
comandos y las lecturas por un bus de consultas; un **manejador (handler)** es la función que de hecho
procesa un comando o una consulta.

::: listing tests/conftest.py | Listado 16.3 — conftest.py: componentes reales del framework cableados sin mocks
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Make the sample's `src/` importable
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))

from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets import (
    DepositFundsHandler,
    GetBalanceHandler,
    GetWalletHandler,
    ListRichWalletsHandler,
    ListWalletsHandler,
    OpenWalletHandler,
    WithdrawFundsHandler,
)
from lumen.models.entities.v1.wallet_orm import WalletEntity
from lumen.models.repositories import WalletRepository

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus, HandlerRegistry
from pyfly.data.relational.sqlalchemy import Base
from pyfly.data.relational.sqlalchemy.post_processor import (
    RepositoryBeanPostProcessor,
)
from pyfly.eda.adapters.memory import InMemoryEventBus


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An in-memory SQLite engine + session factory, schema created.

    Mirrors the framework's relational auto-configuration: build the
    async engine and run Base.metadata.create_all.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[WalletRepository]:
    """The framework WalletRepository, post-processed.

    RepositoryBeanPostProcessor compiles derived-query stubs
    (e.g. find_by_owner_id) onto the bean — the same step the
    ApplicationContext runs at startup.
    """
    session = session_factory()
    repo = WalletRepository(WalletEntity, session)
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    try:
        yield repo
    finally:
        await session.close()


@pytest_asyncio.fixture
async def event_bus() -> AsyncIterator[InMemoryEventBus]:
    """A real in-memory EDA bus — the same EventPublisher used in
    production."""
    yield InMemoryEventBus()


@pytest_asyncio.fixture
async def audit_listener(
    event_bus: InMemoryEventBus,
) -> AsyncIterator[WalletAuditListener]:
    """The wallet audit projection, subscribed to the bus exactly as the
    ApplicationContext auto-wires it at startup."""
    listener = WalletAuditListener()
    method = listener.on_wallet_event
    for pattern in method.__pyfly_event_patterns__:
        event_bus.subscribe(pattern, method)
    yield listener


@pytest_asyncio.fixture
async def command_bus(
    repository: WalletRepository,
    event_bus: InMemoryEventBus,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[DefaultCommandBus]:
    registry = HandlerRegistry()
    registry.register_command_handler(
        OpenWalletHandler(
            repository=repository,
            events=event_bus,
            session_factory=session_factory,
        )
    )
    registry.register_command_handler(
        DepositFundsHandler(
            repository=repository,
            events=event_bus,
            session_factory=session_factory,
        )
    )
    registry.register_command_handler(
        WithdrawFundsHandler(
            repository=repository,
            events=event_bus,
            session_factory=session_factory,
        )
    )
    yield DefaultCommandBus(registry=registry)


@pytest_asyncio.fixture
async def query_bus(
    repository: WalletRepository,
) -> AsyncIterator[DefaultQueryBus]:
    registry = HandlerRegistry()
    registry.register_query_handler(GetWalletHandler(repository=repository))
    registry.register_query_handler(GetBalanceHandler(repository=repository))
    registry.register_query_handler(
        ListWalletsHandler(repository=repository)
    )
    registry.register_query_handler(
        ListRichWalletsHandler(repository=repository)
    )
    yield DefaultQueryBus(registry=registry)
:::

Cada fixture se declara con `@pytest_asyncio.fixture` (no con el `@pytest.fixture` pelado) para que pytest-asyncio gestione el ciclo de vida del iterador asíncrono. `asyncio_mode = "auto"` en `pyproject.toml` hace que los fixtures y las pruebas asíncronas funcionen sin decoradores por función, pero el propio decorador del fixture debe seguir siendo `pytest_asyncio.fixture`.

El fixture `session_factory` es compartido. Tanto `repository` como `command_bus` lo reciben, así que el mismo motor SQLite en memoria respalda las lecturas, las escrituras y la frontera `@transactional` que abren los manejadores. Los fixtures `audit_listener` y `command_bus` reciben ambos `event_bus`; pytest lo instancia una vez por prueba y lo comparte entre ellos, así que los eventos publicados por los manejadores de comandos son visibles para el listener.

*Qué acaba de pasar.* Has cableado un stack de pruebas completo, con forma de producción —motor,
repositorio, bus de eventos, listener y ambos buses de CQRS— enteramente desde fixtures, sin
mocks. Los dos hechos que lo hacen funcionar merecen retenerse. Primero, **los fixtures
se componen por nombre**: `command_bus` pide `repository`, `event_bus` y
`session_factory` simplemente nombrándolos como parámetros, y pytest entrelaza el grafo
por ti. Segundo, **un fixture solicitado por otros dos se construye una vez por prueba**: tanto
`repository` como `command_bus` nombran `session_factory`, así que comparten un solo motor;
la escritura que hace un comando es la lectura que ve una consulta. Acierta con este
archivo y cada prueba de las siguientes cuatro secciones será una llamada de dos líneas.

!!! spring "Equivalencia con Spring"
    `conftest.py` es el `@TestConfiguration` de PyFly más el compartido
    `application-test.properties` de un proyecto Spring Boot: un único lugar que
    declara los beans que cada prueba reutiliza. Un `@pytest_asyncio.fixture` es el equivalente
    aproximado de un método `@Bean` en esa configuración: pytest lo construye de forma perezosa, lo inyecta
    donde se solicita su nombre y lo desmonta después, igual que el contexto de pruebas de Spring
    gestiona el ciclo de vida y la inyección de los beans.

!!! tip "Nada de mocks en ningún sitio"
    Cada componente de `conftest.py` es la implementación real de producción.
    `WalletRepository` es la misma clase que arranca la aplicación.
    `RepositoryBeanPostProcessor` es el mismo post-procesador que el
    `ApplicationContext` ejecuta al arrancar para compilar los esbozos de consulta derivada.
    `InMemoryEventBus` es el mismo bus que se usa en despliegues sin Kafka. El
    objetivo es probar los caminos de código reales, no el cableado.

---

## Pruebas del flujo CQRS de extremo a extremo

Con los fixtures de `conftest.py`, ejercitar el ciclo completo de comando/consulta es cuestión de llamar a `command_bus.send(...)` y `query_bus.query(...)`. No se instancia ningún manejador en el cuerpo de la prueba: el bus despacha al manejador ya registrado en el fixture.

Fíjate en lo corto que se vuelve el cuerpo de cada prueba ahora que el cableado vive en `conftest.py`.
Una prueba declara los fixtures que necesita como parámetros y luego se lee como una narración llana:

**Paso 1 — solicitar los buses.** La firma de cada prueba enumera `command_bus` o
`query_bus`. pytest ve esos nombres, construye el grafo de fixtures desde `conftest.py`
e inyecta los buses listos.

**Paso 2 — enviar comandos.** `await command_bus.send(OpenWallet(...))` devuelve el id del nuevo
monedero; los comandos `DepositFunds` y `WithdrawFunds` posteriores devuelven el saldo
acumulado. Compruebas cada valor de retorno sobre la marcha.

**Paso 3 — consultar el lado de lectura.** `await query_bus.query(GetWallet(...))` y
`GetBalance(...)` recargan el estado persistido y devuelven DTO (objetos de transferencia de datos,
modelos de lectura simples). Compruebas que sus campos coinciden con lo que escribieron los comandos.

**Paso 4 — demostrar los caminos de error.** Las pruebas restantes envían un comando que debe fallar
—un descubierto, un depósito no positivo, un monedero desconocido— envuelto en
`pytest.raises(CommandProcessingException)`. Ese gestor de contexto comprueba que el bloque
lanza la excepción nombrada; si no lo hace, la prueba falla.

::: listing tests/test_cqrs_flow.py | Listado 16.4 — Pruebas CQRS de extremo a extremo a través del bus real
from __future__ import annotations

import pytest
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus


@pytest.mark.asyncio
async def test_full_wallet_lifecycle(
    command_bus: DefaultCommandBus,
    query_bus: DefaultQueryBus,
) -> None:
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-1", currency=Currency.EUR)
    )
    assert isinstance(wallet_id, str) and wallet_id.startswith("wlt-")

    balance = await command_bus.send(
        DepositFunds(wallet_id=wallet_id, amount=1500)
    )
    assert balance == 1500

    balance = await command_bus.send(
        WithdrawFunds(wallet_id=wallet_id, amount=500)
    )
    assert balance == 1000

    wallet = await query_bus.query(GetWallet(wallet_id=wallet_id))
    assert wallet is not None
    assert wallet.id == wallet_id
    assert wallet.owner_id == "u-1"
    assert wallet.currency is Currency.EUR
    assert wallet.balance_minor == 1000
    assert wallet.balance == 10.0

    balance_dto = await query_bus.query(GetBalance(wallet_id=wallet_id))
    assert balance_dto is not None
    assert balance_dto.balance_minor == 1000
    assert balance_dto.balance == 10.0


@pytest.mark.asyncio
async def test_get_wallet_returns_none_for_unknown_id(
    query_bus: DefaultQueryBus,
) -> None:
    assert await query_bus.query(
        GetWallet(wallet_id="wlt-does-not-exist")
    ) is None


@pytest.mark.asyncio
async def test_overdraw_is_rejected_through_the_bus(
    command_bus: DefaultCommandBus,
) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-2", currency=Currency.EUR)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=100))

    with pytest.raises(CommandProcessingException):
        await command_bus.send(
            WithdrawFunds(wallet_id=wallet_id, amount=999)
        )


@pytest.mark.asyncio
async def test_validation_rejects_non_positive_deposit(
    command_bus: DefaultCommandBus,
) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-3", currency=Currency.EUR)
    )
    with pytest.raises(CommandProcessingException):
        await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=0))


@pytest.mark.asyncio
async def test_deposit_to_unknown_wallet_is_rejected(
    command_bus: DefaultCommandBus,
) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    with pytest.raises(CommandProcessingException):
        await command_bus.send(
            DepositFunds(wallet_id="wlt-nope", amount=100)
        )
:::

`test_full_wallet_lifecycle` es la prueba de humo principal: envía cada comando en el orden natural y luego consulta tanto el DTO completo del monedero como el DTO de saldo. El DTO del monedero expone `balance_minor` (unidades menores enteras) y `balance` (unidades mayores como float); ambos derivan de la misma fila `WalletEntity` almacenada a través del repositorio.

Las pruebas de los caminos de error verifican que el bus aflora correctamente las violaciones de dominio. **`CommandProcessingException`** es el envoltorio del bus para cualquier excepción lanzada dentro de un manejador, incluida `BusinessRuleViolation` del agregado. El código que llama nunca ve la excepción de dominio en bruto; siempre ve el envoltorio del bus.

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_cqrs_flow.py -q
```

Salida esperada:

```text
.....                                                                    [100%]
5 passed in 0.05s
```

*Qué acaba de pasar.* Esta es la primera capa que toca infraestructura real, y
aun así se ejecuta en milisegundos. El comando pasó por el bus real, el manejador
real abrió una unidad de trabajo `@transactional` real sobre una sesión SQLite real,
la confirmó (commit), y la consulta la leyó de vuelta: el camino exacto que se ejecuta en producción,
menos la capa HTTP. Como el manejador se registra en el fixture en lugar de
construirse en la prueba, estás probando el *despacho* además de la lógica: si el
enrutamiento de comando a manejador se rompiera, estas pruebas lo atraparían.

!!! note "asyncio_mode = \"auto\" y @pytest.mark.asyncio"
    Con `asyncio_mode = "auto"`, cada prueba asíncrona se recopila y ejecuta
    automáticamente. El decorador `@pytest.mark.asyncio` **no es obligatorio** pero
    es inocuo y hace que la intención asíncrona resulte explícita de un vistazo. Lumen lo conserva
    por claridad.

---

## Pruebas del adaptador del repositorio

Las pruebas de flujo CQRS demuestran la canalización completa de apertura/depósito/retiro/consulta. Las pruebas del adaptador del repositorio van un nivel más hondo: ejercitan directamente la API de `WalletRepository` —CRUD, **consultas derivadas** compiladas a partir de nombres de método, paginación con **`Pageable`/`Page`** y predicados de tipo **`Specification`**— contra una base de datos SQLite en un archivo temporal. Sin Docker, sin proceso externo, sin red. El fixture integrado `tmp_path` de pytest proporciona un directorio temporal que se limpia automáticamente tras cada prueba.

El fixture local `_make_repo` refleja lo que hace el `ApplicationContext` al arrancar: construir el repositorio y ejecutar `RepositoryBeanPostProcessor.after_init` para compilar los esbozos de consulta derivada. Sin esa llamada, métodos como `find_by_owner_id` lanzarían `NotImplementedError`.

Dos términos antes del listado. Una **consulta derivada** es un método de repositorio cuyo cuerpo
se genera a partir de su *nombre*: `find_by_owner_id` se convierte en `WHERE owner_id = :value`,
sin SQL escrito a mano. Una **Specification** es un objeto predicado reutilizable y componible
que pasas a una consulta —`balance_at_least(1000)` es uno— para filtros demasiado
dinámicos como para incrustarlos en un nombre de método. La **paginación** envuelve ambos: `Pageable.of(page,
size, sort)` describe qué porción quieres, y la consulta devuelve una `Page` que lleva
los elementos más `total`, `total_pages` y `has_next`.

Esta sección usa una base de datos SQLite **basada en archivo** (vía `tmp_path`) en lugar de la
de en memoria, para poder demostrar una propiedad más fuerte: la durabilidad tras una reconexión.
Esta es la forma de cada prueba:

**Paso 1 — `sqlite_factory`: un motor sobre archivo temporal.** El fixture construye un motor SQLite
respaldado por un archivo real bajo el `tmp_path` de pytest (un directorio temporal nuevo por prueba,
autoeliminado después), crea el esquema y entrega tanto la fábrica como la URL.
Entregar la URL es lo que permite que una prueba se reconecte con un motor totalmente nuevo.

**Paso 2 — CRUD y persistencia.**
`test_upsert_inserts_then_updates_and_persists` hace un upsert de una fila, lo repite con
un nuevo saldo para demostrar la actualización en sitio, hace commit, la lee de vuelta y luego desecha el
motor y se reconecta con uno *nuevo* para demostrar que los datos realmente llegaron al disco.

**Paso 3 — el camino de id desconocido.** `test_find_by_id_unknown_returns_none` confirma que un
fallo devuelve `None`, no un error.

**Paso 4 — consulta derivada.** `test_derived_find_by_owner_id` inserta monederos para dos
propietarios y comprueba que `find_by_owner_id("alice")` devuelve solo los de Alice: prueba de que el
post-procesador compiló correctamente la convención de nombres de método.

**Paso 5 — Specification + paginación.**
`test_specification_find_rich_paged_and_sorted` y
`test_find_all_pageable_counts_and_pages` ejercitan el camino del predicado `find_rich` /
`find_all_by_spec` y el camino de paginación `find_all(pageable)`, comprobando
`total`, `total_pages`, `has_next` y el orden exacto de `page.items`.

::: listing tests/test_sql_wallet_repository.py | Listado 16.5 — Pruebas de repositorio: CRUD, consulta derivada, paginación, Specification
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen.models.entities.v1.wallet_orm import WalletEntity
from lumen.models.repositories.wallet_repository import (
    WalletRepository,
    balance_at_least,
)
from pyfly.data import Pageable, Sort
from pyfly.data.relational.sqlalchemy import Base
from pyfly.data.relational.sqlalchemy.post_processor import (
    RepositoryBeanPostProcessor,
)


def _entity(
    wid: str,
    owner: str,
    minor: int,
    *,
    currency: str = "EUR",
    age_days: int = 0,
) -> WalletEntity:
    created = datetime.now(UTC) - timedelta(days=age_days)
    return WalletEntity(
        id=wid,
        owner_id=owner,
        currency=currency,
        balance_minor=minor,
        created_at=created,
    )


def _make_repo(session: AsyncSession) -> WalletRepository:
    repo = WalletRepository(WalletEntity, session)
    # Mirror the ApplicationContext: compile derived-query stubs.
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    return repo


@pytest_asyncio.fixture
async def sqlite_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """A temp-file SQLite engine + session factory, schema created.

    Yields the session factory and the database URL so the test can
    reconnect with a fresh engine to verify true persistence.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'wallets.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, db_url
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates_and_persists(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, db_url = sqlite_factory

    async with factory() as session:
        repo = _make_repo(session)
        await repo.upsert(_entity("wlt-1", "owner-42", 0, currency="USD"))
        # update: same PK, new balance
        await repo.upsert(_entity("wlt-1", "owner-42", 2500, currency="USD"))
        await session.commit()

        got = await repo.find_by_id("wlt-1")
        assert got is not None
        assert got.owner_id == "owner-42"
        assert got.currency == "USD"
        assert got.balance_minor == 2500
        assert await repo.count() == 1

    # prove persistence: reconnect with a brand-new engine/session
    fresh_engine = create_async_engine(db_url)
    fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_factory() as fresh_session:
            fresh_repo = _make_repo(fresh_session)
            persisted = await fresh_repo.find_by_id("wlt-1")
            assert persisted is not None, "wallet should survive a reconnect"
            assert persisted.balance_minor == 2500
    finally:
        await fresh_engine.dispose()


@pytest.mark.asyncio
async def test_find_by_id_unknown_returns_none(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        assert await repo.find_by_id("wlt-nope") is None


@pytest.mark.asyncio
async def test_derived_find_by_owner_id(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        await repo.upsert(_entity("wlt-1", "alice", 100))
        await repo.upsert(_entity("wlt-2", "alice", 200))
        await repo.upsert(_entity("wlt-3", "bob", 300))
        await session.commit()

        owned = await repo.find_by_owner_id("alice")
        assert sorted(w.id for w in owned) == ["wlt-1", "wlt-2"]
        assert await repo.find_by_owner_id("nobody") == []


@pytest.mark.asyncio
async def test_specification_find_rich_paged_and_sorted(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        # age_days drives created_at so we can assert newest-first ordering.
        await repo.upsert(_entity("wlt-poor", "a", 50, age_days=3))
        await repo.upsert(_entity("wlt-mid", "b", 1000, age_days=2))
        await repo.upsert(_entity("wlt-rich", "c", 5000, age_days=1))
        await session.commit()

        # Specification: balance_minor >= 1000, newest first, page size 1.
        newest_first = Sort.by("created_at").descending()
        page = await repo.find_rich(1000, Pageable.of(1, 1, newest_first))
        assert page.total == 2          # mid + rich
        assert page.total_pages == 2
        assert page.has_next is True
        assert [w.id for w in page.items] == ["wlt-rich"]

        page2 = await repo.find_rich(1000, Pageable.of(2, 1, newest_first))
        assert [w.id for w in page2.items] == ["wlt-mid"]

        # The bare predicate also works through find_all_by_spec.
        rich = await repo.find_all_by_spec(balance_at_least(5000))
        assert [w.id for w in rich] == ["wlt-rich"]


@pytest.mark.asyncio
async def test_find_all_pageable_counts_and_pages(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        for i in range(5):
            await repo.upsert(
                _entity(f"wlt-{i}", "owner", i * 100, age_days=5 - i)
            )
        await session.commit()

        page = await repo.find_all(
            Pageable.of(1, 2, Sort.by("created_at").descending())
        )
        assert page.total == 5
        assert page.total_pages == 3
        assert len(page.items) == 2
        # newest first -> wlt-4 (age 1 day), then wlt-3
        assert [w.id for w in page.items] == ["wlt-4", "wlt-3"]
:::

Cuatro cosas a destacar. Primero, `_make_repo` llama a `RepositoryBeanPostProcessor().after_init(repo, ...)`; sin esto, `find_by_owner_id` sigue siendo un esbozo y lanza `NotImplementedError`. El post-procesador compila el nombre de método en una cláusula `WHERE owner_id = :owner_id` de SQLAlchemy. Segundo, `upsert` es la inserción-o-actualización del repositorio; tras cada lote de upserts, `await session.commit()` vuelca a SQLite. Tercero, `find_rich` toma un saldo mínimo y un `Pageable`; delega en `find_all_by_spec_paged(balance_at_least(min), pageable)`. Cuarto, el patrón de dos motores en `test_upsert_inserts_then_updates_and_persists` demuestra la durabilidad verdadera: los datos confirmados a través de un motor son legibles por un motor y una sesión completamente nuevos.

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_sql_wallet_repository.py -q
```

Salida esperada:

```text
.....                                                                    [100%]
5 passed in 0.06s
```

*Qué acaba de pasar.* Lo más notable es la reconexión de dos motores en la primera prueba.
Muchas pruebas de "persistencia" pasan incluso cuando no se ha escrito nada en el disco, porque la misma
sesión cachea el objeto en memoria y lo devuelve en la lectura. Al desechar el
motor por completo y abrir un *segundo* motor contra la misma URL de archivo, esta prueba
fuerza un viaje de ida y vuelta real al almacenamiento; si `upsert` o `commit` no estuvieran
persistiendo silenciosamente, la reconexión devolvería `None` y la prueba fallaría. Esa es la
diferencia entre probar tu código y probar tu caché.

!!! spring "Equivalencia con Spring"
    Esta capa de prueba es el equivalente en Python de `@DataJpaTest` con una base de datos
    H2 embebida en Spring Boot. `@DataJpaTest` carga solo la capa JPA (entidades,
    repositorios, Flyway) y cablea una H2 en memoria nueva para cada clase de prueba.
    El fixture `sqlite_factory` hace lo mismo: crear el esquema, ejecutar las pruebas,
    desechar el motor. Sin Docker, sin proceso externo.

!!! tip "Las consultas derivadas son convenciones de nombre de método"
    `WalletRepository.find_by_owner_id` se declara como un esbozo
    (`raise NotImplementedError`). `RepositoryBeanPostProcessor` inspecciona el
    nombre del método al arrancar —`find_by_owner_id` → `WHERE owner_id = :value`—
    y reemplaza el esbozo por una corrutina real. Por tanto, probar este método
    también prueba que la convención del post-procesador funciona correctamente.

---

## Pruebas del listener de eventos

`WalletAuditListener` escucha los eventos de dominio publicados por los manejadores de comandos. Probarlo de extremo a extremo —el comando se ejecuta en el bus, el manejador publica eventos, el listener los recibe— requiere que los tres componentes compartan el mismo `InMemoryEventBus`. Los fixtures de `conftest.py` ya lo disponen: tanto `command_bus` como `audit_listener` aceptan un argumento `event_bus`, y pytest inyecta la misma instancia en ambos.

Un **listener de eventos** es simplemente un método que el framework suscribe al bus para que se
ejecute cada vez que se publica un evento coincidente. El `WalletAuditListener` de Lumen mantiene un
registro de auditoría en memoria y un saldo acumulado por monedero: una pequeña **proyección** (un modelo de
lectura construido plegando eventos). Probarlo es la demostración más clara del
truco del fixture compartido: como `command_bus` y `audit_listener` nombran el mismo
`event_bus`, un evento que publica un comando es un evento que observa el listener, sin
pegamento alguno en el cuerpo de la prueba.

Las pruebas siguen un solo ritmo:

**Paso 1 — recorrer comandos.** Abrir un monedero, depositar, retirar; todo a través de
`command_bus`.

**Paso 2 — leer la proyección.** Llamar a `audit_listener.entries_for(wallet_id)` y
comprobar los tipos de evento registrados, en orden, más el `running_total`.

**Paso 3 — comprobar el negativo.** Una prueba deja deliberadamente el saldo en descubierto —un comando que
debe fallar— y comprueba que el registro de auditoría no anota nada de él. Una operación fallida
no deja rastro.

::: listing tests/test_event_listener.py | Listado 16.6 — Pruebas del listener de eventos: el comando publica, el listener observa
from __future__ import annotations

import pytest
from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus


@pytest.mark.asyncio
async def test_listener_observes_wallet_events(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-1", currency=Currency.EUR)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=1500))
    await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=400))

    entries = audit_listener.entries_for(wallet_id)
    assert [e.event_type for e in entries] == [
        "WalletOpened",
        "FundsDeposited",
        "FundsWithdrawn",
    ]

    # The payload carried the real domain-event fields.
    deposited = entries[1]
    assert deposited.payload["amount"] == 1500
    assert deposited.payload["currency"] == "EUR"
    assert deposited.payload["balance"] == 1500
    assert deposited.event_id  # the aggregate's DomainEvent.event_id

    # The running-total projection reflects deposit − withdrawal.
    assert audit_listener.running_total(wallet_id) == 1100


@pytest.mark.asyncio
async def test_listener_records_nothing_before_any_command(
    audit_listener: WalletAuditListener,
) -> None:
    assert audit_listener.entries == []
    assert audit_listener.running_total("anything") == 0


@pytest.mark.asyncio
async def test_event_type_matches_domain_event_class_names(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    # A rejected withdrawal raises no event — it must not appear in the log.
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-2", currency=Currency.USD)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=100))

    from pyfly.cqrs.exceptions import CommandProcessingException

    with pytest.raises(CommandProcessingException):
        await command_bus.send(
            WithdrawFunds(wallet_id=wallet_id, amount=9999)
        )

    types = [e.event_type for e in audit_listener.entries_for(wallet_id)]
    assert types == ["WalletOpened", "FundsDeposited"]
    assert audit_listener.running_total(wallet_id) == 100
:::

`test_listener_observes_wallet_events` es la prueba de integración central: tres comandos producen tres eventos, el listener los registra los tres en orden, los campos del payload coinciden con los campos de la dataclass de evento del agregado y la proyección `running_total` es igual al resultado aritmético. Sin mock del bus, sin lista de captura de eventos: el listener de producción se ejecuta sobre el bus de producción.

`test_event_type_matches_domain_event_class_names` demuestra un invariante de dominio: un comando rechazado (descubierto) no lanza ningún evento. El registro de auditoría nunca debe anotar un efecto secundario de una operación fallida.

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_event_listener.py -q
```

Salida esperada:

```text
...                                                                      [100%]
3 passed in 0.04s
```

*Qué acaba de pasar.* Ninguna parte de la prueba conectó el listener al bus: el
fixture `audit_listener` hizo eso en `conftest.py`, suscribiendo el manejador al
mismo `event_bus` por el que publica el `command_bus`. Así que enviar un comando y luego
leer `entries_for(...)` ejercita el camino real de publicación/suscripción de extremo a extremo. La
prueba negativa es la sutil: demuestra que el registro de auditoría se rige por *eventos*, no
por *intentos*; un retiro rechazado lanza un `BusinessRuleViolation` antes de que se emita ningún
evento, así que nada llega al listener.

!!! tip "event_type es el nombre de la clase"
    El publicador de eventos de PyFly fija `event_type` al nombre de la clase del evento de dominio:
    `"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"`. El
    decorador `@event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])`
    sobre `WalletAuditListener.on_wallet_event` nombra esos tres tipos
    explícitamente; el framework los almacena en el método como
    `__pyfly_event_patterns__`, que el fixture `audit_listener` lee para suscribirse.
    La prueba comprueba directamente las cadenas con los nombres de clase.

---

## Prueba de integración con el contexto arrancado

Las pruebas unitarias, las de flujo CQRS y las de repositorio cablean cada una una capa del stack. La prueba de integración con el contexto arrancado las cablea todas a la vez: inicia el `ApplicationContext` real —escaneo de componentes de inyección de dependencias, autoconfiguración de CQRS, autoconfiguración relacional, `RepositoryBeanPostProcessor`, la costura `@transactional`, el bus de eventos de la EDA— y luego resuelve el `DefaultCommandBus` y el `DefaultQueryBus` desde el contexto y recorre el ciclo de vida completo del monedero.

El **ApplicationContext** es el contenedor en tiempo de ejecución de PyFly: el objeto que escanea en busca de
componentes, construye beans, ejecuta post-procesadores y mantiene unida la aplicación
cableada. Arrancarlo es la prueba más fiel que puedes escribir sin llegar a iniciar un
servidor HTTP: cada pieza de cableado que el framework hace al arrancar ocurre de verdad.

La URL de la base de datos se sustituye mediante una variable de entorno para que la prueba nunca toque el `lumen.db` del desarrollador. Este es el plan:

**Paso 1 — aislar la base de datos.** El fixture `booted_context` usa el fixture
`monkeypatch` de pytest para fijar `PYFLY_DATA_RELATIONAL_URL` a una ruta SQLite de archivo temporal
bajo `tmp_path`, *antes* de que la aplicación arranque. `monkeypatch` es la forma segura de pytest de fijar una
variable de entorno durante una sola prueba y restaurarla automáticamente
después, así que esta prueba nunca puede pisar tu `lumen.db` real.

**Paso 2 — arrancar la aplicación real.** Construye `PyFlyApplication(LumenApplication,
config_path=...)` y `await app.startup()`. Esa única llamada ejecuta toda la secuencia de arranque:
escaneo de componentes, todas las autoconfiguraciones, el `RepositoryBeanPostProcessor`
y el bus de eventos. El fixture entrega `app.context` y, en su bloque `finally`,
cierra la sesión compartida y llama a `app.shutdown()`.

**Paso 3 — resolver beans y recorrer el ciclo de vida.** La prueba llama a
`ctx.get_bean(DefaultCommandBus)` y `ctx.get_bean(DefaultQueryBus)` —obteniendo los
*mismos* buses que usaría la aplicación— y luego ejecuta apertura → depósito → retiro → listado →
ricos → saldo, comprobando cada resultado.

::: listing tests/test_app_context_integration.py | Listado 16.7 — Integración con el contexto arrancado: composición completa de inyección de dependencias + persistencia
from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))


@pytest_asyncio.fixture
async def booted_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[object]:
    """Boot the full LumenApplication against an isolated SQLite file."""
    db_path = tmp_path / "lumen-it.db"
    monkeypatch.setenv(
        "PYFLY_DATA_RELATIONAL_URL",
        f"sqlite+aiosqlite:///{db_path}",
    )
    # Silence the pool-GC warning that aiosqlite emits at teardown.
    logging.getLogger(
        "sqlalchemy.pool.impl.AsyncAdaptedQueuePool"
    ).setLevel(logging.CRITICAL)

    from lumen.app import LumenApplication
    from pyfly.core import PyFlyApplication

    app = PyFlyApplication(
        LumenApplication,
        config_path=str(_HERE.parent / "pyfly.yaml"),
    )
    await app.startup()
    try:
        yield app.context
    finally:
        await app.context.get_bean(AsyncSession).close()
        await app.shutdown()


@pytest.mark.asyncio
async def test_full_lifecycle_through_booted_context(
    booted_context: object,
) -> None:
    from lumen.core.services.wallets.deposit_funds_command import DepositFunds
    from lumen.core.services.wallets.get_balance_query import GetBalance
    from lumen.core.services.wallets.get_wallet_query import GetWallet
    from lumen.core.services.wallets.list_rich_wallets_query import (
        ListRichWallets,
    )
    from lumen.core.services.wallets.list_wallets_query import ListWallets
    from lumen.core.services.wallets.open_wallet_command import OpenWallet
    from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
    from lumen.interfaces.enums.v1.currency import Currency
    from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
    from pyfly.data import Pageable

    ctx = booted_context
    commands = ctx.get_bean(DefaultCommandBus)
    queries = ctx.get_bean(DefaultQueryBus)

    # --- open -> deposit -> withdraw, each a committed unit of work -------
    w1 = await commands.send(OpenWallet(owner_id="u-1", currency=Currency.EUR))
    w2 = await commands.send(OpenWallet(owner_id="u-2", currency=Currency.EUR))
    assert w1.startswith("wlt-") and w2.startswith("wlt-")

    assert await commands.send(DepositFunds(wallet_id=w1, amount=5000)) == 5000
    assert await commands.send(WithdrawFunds(wallet_id=w1, amount=1500)) == 3500
    assert await commands.send(DepositFunds(wallet_id=w2, amount=100)) == 100

    # --- persistence survived: reload the aggregate via the query side ---
    reloaded = await queries.query(GetWallet(wallet_id=w1))
    assert reloaded is not None
    assert reloaded.owner_id == "u-1"
    assert reloaded.balance_minor == 3500

    # --- paged list (find_all(pageable) + Page.map) ----------------------
    page = await queries.query(ListWallets(pageable=Pageable.of(1, 10)))
    assert page.total == 2
    assert {w.id for w in page.items} == {w1, w2}

    # --- Specification: only wallets with balance >= 1000 ----------------
    rich = await queries.query(
        ListRichWallets(min_minor=1000, pageable=Pageable.of(1, 10))
    )
    assert rich.total == 1
    assert [w.id for w in rich.items] == [w1]

    everyone = await queries.query(
        ListRichWallets(min_minor=0, pageable=Pageable.of(1, 10))
    )
    assert everyone.total == 2

    # --- projection-backed balance ---------------------------------------
    balance = await queries.query(GetBalance(wallet_id=w1))
    assert balance is not None
    assert balance.balance_minor == 3500
    assert balance.balance == 35.0
:::

`booted_context` usa el fixture integrado `monkeypatch` de pytest para fijar `PYFLY_DATA_RELATIONAL_URL` antes de que la aplicación arranque. El framework lee esta variable de entorno durante la autoconfiguración relacional, así que el contexto usa la base de datos SQLite de archivo temporal aislada durante la vida de la prueba, y luego la desecha cuando el fixture se desmonta.

`test_full_lifecycle_through_booted_context` ejercita cada tipo de consulta que la aplicación expone: `GetWallet` (recarga del agregado), `ListWallets` (listado paginado usando `find_all(pageable)`), `ListRichWallets` (predicado de tipo Specification usando `find_all_by_spec_paged`) y `GetBalance` (saldo respaldado por proyección). Demuestra que el `RepositoryBeanPostProcessor`, la frontera `@transactional` alrededor de cada manejador de comandos y el cableado de inyección de dependencias se componen todos correctamente en un solo arranque.

**Ejecútalo.**

```bash
uv run --extra dev pytest tests/test_app_context_integration.py -q
```

Salida esperada:

```text
.                                                                        [100%]
1 passed in 0.15s
```

Una prueba, pero la más pesada de la suite: realmente arrancó el framework. Si
el escaneo de inyección de dependencias se saltó un bean, una autoconfiguración cableó mal o la frontera
`@transactional` no consiguió hacer commit, esta es la prueba que lo atrapa, lo cual es exactamente por lo que
se sitúa en la cima de la pirámide y por lo que solo hay una de ella.

*Qué acaba de pasar.* La sustitución de la variable de entorno es el truco que lo sostiene todo.
El framework lee `pyfly.data.relational.url` de la configuración durante la autoconfiguración
relacional, y PyFly mapea cualquier clave de configuración a una variable de entorno `PYFLY_*`
(los puntos y los guiones se vuelven guiones bajos, en mayúsculas), así que `PYFLY_DATA_RELATIONAL_URL`
sustituye la `url` de `pyfly.yaml`. Fijarla con `monkeypatch` *antes* de
`app.startup()` es lo que redirige toda la aplicación arrancada a una base de datos
desechable, y `monkeypatch` deshace el cambio cuando el fixture se desmonta, así que ninguna
otra prueba se ve afectada.

!!! tip "Un perfil de prueba dedicado (v26.6.110)"
    Fijar una variable está bien para una sola sustitución. Cuando un proyecto necesita un bloque
    entero de ajustes solo de prueba, el mecanismo de **perfiles** de PyFly (equivalencia con Spring) es
    más limpio: deja un `pyfly-test.yaml` junto a `pyfly.yaml` con tus sustituciones de prueba,
    luego actívalo fijando `PYFLY_PROFILES_ACTIVE=test` (o
    `pyfly.profiles.active: test` en la configuración). Al arrancar, PyFly superpone
    `pyfly-test.yaml` sobre el `pyfly.yaml` base, así que valores como la URL de la base de datos
    o `ddl-auto` se aplican solo bajo ese perfil. Lumen no necesita un perfil
    —una sola sustitución de entorno cubre su único ajuste solo de prueba—, pero recurre a uno a medida que
    la configuración de pruebas crezca.

!!! spring "Equivalencia con Spring"
    Esta prueba es el equivalente en Python de `@SpringBootTest` con una base de datos H2
    embebida. `@SpringBootTest` carga el contexto de aplicación completo, incluidas todas las
    autoconfiguraciones y la capa JPA; fijas `spring.datasource.url` en
    `application-test.properties` para redirigir a H2. La sustitución de variable de entorno
    de PyFly (`monkeypatch.setenv`) cumple el mismo papel. Ambos
    enfoques demuestran que la aplicación compuesta funciona de extremo a extremo sin
    infraestructura externa alguna.

---

## Ayudantes de pruebas del framework

Las pruebas de arriba cubren la pirámide completa de Lumen con primitivas estándar de pytest y los componentes reales de producción de PyFly. Para aplicaciones más grandes o equipos que prefieren más estructura, `pyfly.testing` incluye ayudantes de más alto nivel que reflejan las anotaciones de pruebas de Spring Boot.

**`PyFlyTestCase` + `mock_bean(T)`** funcionan como `@MockBean` en `@SpringBootTest`. Declara `repo = mock_bean(WalletDomainRepository)` en el cuerpo de la clase; `setup()` instala un `AsyncMock(spec=T)` en el contexto de aplicación y lo cablea en cualquier colaborador que dependa de él.

**`create_test_container(overrides={Interface: Implementation})`** construye un contenedor de inyección de dependencias con dobles (fakes) registrados para interfaces concretas. Resuelve desde él la clase bajo prueba y sus dependencias ya quedan inyectadas.

**`assert_event_published(events, event_type, payload_contains=...)`** rastrea una lista capturada de `EventEnvelope` en busca del primer sobre del tipo dado, opcionalmente comprueba claves del payload y devuelve el sobre para aserciones posteriores. `assert_no_events_published(events)` falla si la lista no está vacía.

**Integración con Testcontainers** (`postgres_container()`, `redis_container()`, `pyfly_config(container, base={...})`) es el equivalente de PyFly a `@Testcontainers` + `@ServiceConnection` de Spring Boot. Arranca un contenedor Postgres real; `pyfly_config` reescribe la URL síncrona `psycopg2://` a `postgresql+asyncpg://` y la fusiona en un `Config` listo para arrancar un `ApplicationContext`. Instala el soporte con:

```bash
pip install 'pyfly[testcontainers]'
```

Protege cada prueba de Testcontainers con `@requires_docker` para que se omita limpiamente en máquinas sin Docker y se ejecute automáticamente en los ejecutores de CI que sí lo tengan:

```python
from pyfly.testing import postgres_container, pyfly_config, requires_docker

@requires_docker
async def test_wallet_round_trip_against_real_postgres():
    with postgres_container() as pg:
        config = pyfly_config(pg, base={"pyfly.data.enabled": True})
        assert config.get("pyfly.data.relational.url").startswith(
            "postgresql+asyncpg://"
        )
        ...
```

Lumen no usa estos ayudantes: SQLite cubre la capa de persistencia sin Docker, y el bus en memoria cubre el enrutamiento de eventos. Recurre a ellos cuando tu proyecto tenga infraestructura que no pueda reproducirse sin un demonio real.

**Ejecútalo.** Ya has recorrido cada capa archivo a archivo. Ejecuta toda la suite una vez
más para confirmar que la pirámide completa está en verde en conjunto:

```bash
uv run --extra dev pytest -q
```

Salida esperada: el mismo `41 passed` con el que empezaste, ahora con un modelo mental de
exactamente lo que demuestra cada punto:

```text
.........................................                                [100%]
41 passed in 0.28s
```

---

## Lo que construiste {.recap}

Los seis archivos de prueba que construyó este capítulo suman 26 pruebas superadas, ejercitando cada capa de la pirámide. Junto con las pruebas de la saga del Capítulo 12 y las pruebas de event sourcing del Capítulo 9, la suite completa de Lumen son **41 pruebas superadas**: el recuento que viste cuando ejecutaste `uv run --extra dev pytest -q` al principio.

En la base, `test_money.py` y `test_wallet_aggregate.py` demuestran la aritmética, la inmutabilidad y las reglas de invariante del modelo de dominio. Todas las pruebas son funciones síncronas de Python puro, sin fixtures, sin inyección de dependencias, sin `async`. El atributo `BusinessRuleViolation.rule` hace que cada aserción sea específica del invariante exacto incumplido.

En el centro, `conftest.py` cablea los componentes reales —el `WalletRepository` del framework sobre un motor SQLite en memoria, `InMemoryEventBus`, los cinco manejadores de comandos y consultas (incluidos `ListWalletsHandler` y `ListRichWalletsHandler`) y `WalletAuditListener`— en fixtures asíncronos reutilizables que pytest comparte automáticamente entre módulos. El `RepositoryBeanPostProcessor` se aplica al fixture del repositorio exactamente como el `ApplicationContext` lo aplica al arrancar. `test_cqrs_flow.py` despacha comandos y consultas a través del bus real y comprueba cada campo de los DTO de consulta. `test_event_listener.py` demuestra que el listener de auditoría observa exactamente los eventos producidos por comandos exitosos y nada de los rechazados.

`test_sql_wallet_repository.py` ejercita el `WalletRepository` directamente contra un archivo SQLite temporal, cubriendo la superficie CRUD completa, la consulta derivada `find_by_owner_id` (compilada a partir del nombre del método por `RepositoryBeanPostProcessor`), la API `find_all(pageable)` que devuelve una `Page` con recuento total y metadatos de página, y el camino del predicado de tipo `Specification` vía `find_rich` / `find_all_by_spec`. El patrón de reconexión de dos motores demuestra la durabilidad verdadera.

En la cima, `test_app_context_integration.py` arranca la `LumenApplication` real con la URL de la base de datos sustituida por un archivo SQLite aislado, y luego recorre el ciclo de vida completo de apertura → depósito → retiro → listado → ricos → saldo a través de los buses resueltos por el contexto. Esta única prueba demuestra que el escaneo de inyección de dependencias, la autoconfiguración de CQRS, el `RepositoryBeanPostProcessor` y la frontera `@transactional` se componen todos correctamente.

En concreto, aprendiste:

- **`asyncio_mode = "auto"` + `pythonpath = ["src"]`** en `pyproject.toml`:
  todas las pruebas asíncronas se ejecutan sin decoradores; la disposición `src/` es importable.
- **`uv run --extra dev pytest -q`**: el `uv sync` a secas omite el grupo de desarrollo;
  incluye siempre `--extra dev` para obtener pytest.
- **`@pytest_asyncio.fixture`**: ciclo de vida del fixture asíncrono gestionado por
  pytest-asyncio; el `@pytest.fixture` simple no maneja generadores asíncronos.
- **Instancias de fixture compartidas**: cuando dos fixtures solicitan el mismo nombre de
  fixture (p. ej., `event_bus`, `session_factory`), pytest lo resuelve una vez por
  prueba y comparte la instancia.
- **`RepositoryBeanPostProcessor().after_init(repo, name)`**: debe
  llamarse en las pruebas que ejercitan consultas derivadas; sin él, los esbozos
  de método lanzan `NotImplementedError`.
- **Consultas derivadas** (`find_by_owner_id`): declaradas como esbozos; el
  post-procesador las compila a `WHERE owner_id = :value` al arrancar.
- **`Pageable.of(page, size, sort)` + `Page`**: la API `find_all(pageable)`
  devuelve una `Page` con `total`, `total_pages`, `has_next` e
  `items`; comprueba cada campo para verificar la corrección de la paginación.
- **Predicados de tipo `Specification`**: `balance_at_least(n)` se pasa a
  `find_rich` / `find_all_by_spec` para filtrar por un predicado arbitrario
  sin añadir un nuevo método de consulta derivada.
- **`pending_events()` frente a `clear_events()`**: `pending_events()` lee
  sin vaciar; `clear_events()` vacía. Llama siempre a `clear_events()` en
  los pasos de organización para que las aserciones solo vean eventos del paso de actuación.
- **`BusinessRuleViolation.rule`**: comprueba la cadena de regla exacta, no solo
  la clase de excepción, para demostrar que se disparó el invariante correcto.
- **`monkeypatch.setenv`**: sustituye la configuración antes de arrancar el
  contexto en las pruebas de integración; el framework lee variables de entorno
  durante la autoconfiguración.
- **Sustituciones de configuración `PYFLY_*`**: cada clave de configuración se mapea a una variable de
  entorno (`pyfly.data.relational.url` → `PYFLY_DATA_RELATIONAL_URL`); fíjala
  con `monkeypatch.setenv` antes de arrancar para redirigir toda la aplicación.
- **Perfil de prueba (v26.6.110)**: para un bloque de ajustes solo de prueba, añade un
  superpuesto `pyfly-test.yaml` y actívalo con `PYFLY_PROFILES_ACTIVE=test`
  (equivalencia con el `application-test.yaml` de Spring).
- **Ayudantes del framework** (`PyFlyTestCase`, `mock_bean`, `create_test_container`,
  Testcontainers): disponibles en `pyfly.testing` para proyectos que los necesiten;
  Lumen lo mantiene simple con componentes reales.

---

## Pruébalo tú mismo {.exercises}

1. **Añade una prueba para el retiro de importe cero.** En `test_wallet_aggregate.py`,
   añade `test_withdraw_zero_is_rejected`. Abre un monedero, deposita 500 EUR y luego
   intenta `wallet.withdraw(Money(0, Currency.EUR))`. Comprueba que se lanza una
   `BusinessRuleViolation` y verifica su atributo `.rule`. Compara
   el nombre de la regla con el equivalente del depósito: ¿son simétricas las reglas?

2. **Prueba un monedero desconocido vía el bus de CQRS.** En `test_cqrs_flow.py`, añade
   `test_withdraw_from_unknown_wallet_is_rejected`. Envía solo un
   comando `WithdrawFunds(wallet_id="wlt-ghost", amount=50)` sin abrir un
   monedero antes. Comprueba que se lanza `CommandProcessingException`. Confirma
   que el repositorio sigue sin contener monederos consultando
   `GetWallet(wallet_id="wlt-ghost")` y comprobando `None`.

3. **Amplía la prueba del listener con un segundo monedero.** En
   `test_event_listener.py`, añade una prueba que abra dos monederos (`u-A` y
   `u-B`), deposite importes distintos en cada uno y luego llame a
   `audit_listener.entries_for(wallet_id_A)` y
   `audit_listener.entries_for(wallet_id_B)` por separado. Comprueba que cada uno
   devuelve exactamente dos entradas (`WalletOpened` + `FundsDeposited`) y que
   los valores de `amount` del payload difieren. Esto demuestra que `entries_for` filtra
   por ID de monedero, no por tipo de evento.

4. **Añade una prueba de paginación multipropietario.** En `test_sql_wallet_repository.py`,
   añade una prueba que inserte diez monederos con dos propietarios distintos, llame a
   `find_by_owner_id` para cada propietario y luego llame a `find_all` con
   `Pageable.of(1, 3, Sort.by("balance_minor").descending())`. Comprueba que
   `page.total == 10`, `page.total_pages == 4` y que el primer elemento de
   `page.items` tiene el `balance_minor` más alto. Esto demuestra que la paginación
   es independiente del filtro de consulta derivada.
