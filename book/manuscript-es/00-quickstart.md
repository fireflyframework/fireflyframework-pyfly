<span class="eyebrow">Inicio rápido</span>

# Construye Lumen paso a paso {.chtitle}

::: figure art/openers/ch01.svg | &nbsp;

Bienvenido. Esto es lo primerísimo que vas a construir con PyFly, y lo vamos a hacer con calma. Al final de este capítulo habrás pasado de una *carpeta vacía* a una porción *en ejecución y probada* de un servicio real de monedero digital: abrir un monedero, persistirlo en una base de datos, leer su saldo de vuelta por HTTP y reaccionar a un evento de dominio. Cada concepto recibe un fragmento de código pequeño y completo y un punto de control "Ejecútalo" para que lo veas funcionar antes de seguir adelante.

Esto es un *recorrido*, no la inmersión profunda. Cada paso adelanta un tema que la Parte I y la Parte II tratan a fondo más tarde. El objetivo aquí es ganar impulso: para cuando llegues al Capítulo 1 ya habrás conocido la inyección de dependencias, la configuración, HTTP, la persistencia, CQRS y los eventos —en pequeño— y el resto del libro completará el *porqué*.

La aplicación que construyes se llama **Lumen**: un monedero digital con sabor a DDD. Un monedero puede abrirse, recibir depósitos y permitir retiradas, protegiendo una regla central —**el saldo nunca se vuelve negativo**— y modelando el dinero con aritmética entera exacta para que nunca haya desviaciones de coma flotante. Es la misma aplicación que construye el libro entero, así que nada de lo que aprendas aquí es desechable.

!!! note "Nota"
    Este capítulo está escrito sobre PyFly **v26.6.110**. Cada listado está tomado del proyecto real y en ejecución `samples/lumen` que acompaña al libro: el código compila, arranca y pasa sus pruebas. Reconocerás estos mismos archivos de nuevo, con más profundidad, en capítulos posteriores.

---

## Paso 1 — Requisitos previos e instalación

PyFly es un framework de Python, y la forma más fluida de trabajar con él es a través de [**uv**](https://docs.astral.sh/uv/), el rápido gestor de paquetes y proyectos de Python de Astral. uv se encarga de tu versión de Python, de tu entorno virtual y de tus dependencias en una sola herramienta, y la herramienta de línea de comandos `pyfly` se ejecuta a través de él.

Necesitas dos cosas:

* **Python 3.12 o más reciente.** PyFly usa funciones de tipado modernas (`StrEnum`, uniones `X | None`, genéricos de PEP 695).
* **uv.** Instálalo una vez, en todo el sistema.

Instala uv con la orden oficial de una línea:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# Windows (PowerShell):
#   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Ejecútalo

Confirma que ambas herramientas están disponibles:

```bash
uv --version
uv python install 3.12   # ensures a 3.12+ interpreter is available
```

Deberías ver impresa una versión de uv, y uv informará de que Python 3.12 está instalado (o ya presente).

!!! tip "Siguiendo el ejemplo terminado"
    Todo lo que hay en este capítulo existe, ya terminado, en el repositorio del libro bajo `samples/lumen`. Si en algún momento quieres comparar tu código con el original —o simplemente ejecutarlo—, clona el repositorio y haz:

    ```bash
    cd samples/lumen
    uv sync --extra dev        # framework + pytest
    uv run pyfly run --server uvicorn
    ```

    Puedes construir junto a él, copiando un archivo a la vez, o leer la versión terminada cuando un paso no esté claro. Ambas cosas funcionan.

---

## Paso 2 — Andamiaje del proyecto

PyFly incluye un generador de proyectos, `pyfly new`, igual que Spring tiene el Spring Initializr. Escribe una distribución de proyecto convencional para que no empieces desde una página en blanco. Como `pyfly` vive dentro del paquete del framework, lo primerísimo que hacemos es crear un directorio de proyecto y añadirle PyFly.

Para este recorrido construiremos los directorios a mano a medida que avancemos —así cada archivo queda visible y nada se esconde detrás de un generador—, pero el generador está ahí cuando quieras tomar ventaja:

```bash
pyfly new lumen --archetype hexagonal --features web,data-relational
```

Esa orden crea una carpeta `lumen/`, un `pyproject.toml`, un `pyfly.yaml` y un árbol de fuentes por capas. Creemos la misma forma nosotros mismos para que veas exactamente para qué sirve cada pieza. Empieza con el proyecto y sus dependencias:

```bash
mkdir lumen && cd lumen
uv init --package --name lumen
uv add "pyfly[cli,web,data-relational]" "pydantic>=2.5"
uv add --dev "pytest>=8" "pytest-asyncio>=0.24" "httpx>=0.27"
```

Los tres extras de PyFly que acabas de añadir se corresponden con las tres cosas que Lumen necesita: `cli` aporta la propia orden `pyfly`, `web` aporta el servidor ASGI y `data-relational` aporta SQLAlchemy 2 (async) más `aiosqlite` para que podamos persistir en un archivo SQLite sin ninguna base de datos externa que instalar.

### La distribución del proyecto

Las aplicaciones PyFly siguen una estructura por capas que separa el contrato público, el modelo de dominio, la lógica de aplicación y el borde web. Crea estos paquetes bajo `src/lumen`:

```
lumen/
├── pyproject.toml
├── pyfly.yaml                 # framework configuration
└── src/lumen/
    ├── interfaces/            # the public contract: DTOs + enums
    │   ├── dtos/v1/
    │   └── enums/v1/
    ├── models/                # the domain model + persistence
    │   ├── entities/v1/
    │   └── repositories/
    ├── core/                  # application logic: commands, queries, handlers
    │   ├── services/
    │   └── mappers/
    ├── web/                   # the HTTP edge: controllers
    │   └── controllers/
    ├── app.py                 # the application class
    └── main.py                # the ASGI entry point
```

Cada capa tiene un único cometido. `interfaces` es la frontera con la que habla el resto del código (y otros servicios). `models` contiene los ricos objetos de dominio y las filas en que se persisten. `core` contiene las operaciones de negocio. `web` las expone por HTTP. Las rellenaremos capa por capa.

!!! spring "Equivalencia con Spring"
    `pyfly new` es el equivalente del Spring Initializr (`start.spring.io`). Las funcionalidades `web` y `data-relational` son las contrapartes en PyFly de los starters `spring-boot-starter-web` y `spring-boot-starter-data-jpa`: nombrar una funcionalidad arrastra exactamente las dependencias y la autoconfiguración que esa funcionalidad necesita, y nada más.

### La clase de aplicación

Dos archivos convierten un paquete en una aplicación PyFly. El primero, `app.py`, declara la propia aplicación: qué paquetes escanear en busca de componentes y qué niveles del framework activar.

::: listing lumen/app.py | Listado 0.1 — La clase de aplicación
from __future__ import annotations

from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack


@enable_domain_stack
@pyfly_application(
    name="lumen",
    version="1.0.0",
    description="Lumen — a DDD digital-wallet service built on the PyFly framework.",
    scan_packages=[
        "lumen.models.repositories",
        "lumen.core.services.wallets",
        "lumen.web.controllers",
    ],
)
class LumenApplication:
    pass
:::

`@pyfly_application` marca la clase como una aplicación PyFly y `scan_packages` le dice al contenedor de inyección de dependencias dónde buscar los componentes que declararás: tus repositorios, servicios, manejadores de comandos/consultas y controladores. `@enable_domain_stack` activa los niveles de dominio en los que nos apoyaremos más tarde: CQRS, el motor transaccional, la capa de datos relacional y los eventos.

### El punto de entrada ASGI

El segundo archivo, `main.py`, es lo que un servidor ASGI importa y sirve realmente. Arranca PyFly —carga la configuración, escanea tus paquetes y construye el contexto de aplicación— y luego entrega la aplicación web resultante a Starlette.

::: listing lumen/main.py | Listado 0.2 — El punto de entrada ASGI
from __future__ import annotations

from lumen.app import LumenApplication
from pyfly.core import PyFlyApplication
from pyfly.web.adapters.starlette import create_app

# Bootstrap: load config, scan packages, build the DI context.
_pyfly = PyFlyApplication(LumenApplication)

app = create_app(
    title="lumen",
    version="1.0.0",
    description="Lumen — a DDD digital-wallet service built on the PyFly framework.",
    context=_pyfly.context,
)
:::

!!! note "Nota"
    El `samples/lumen/main.py` real añade un gancho `lifespan` y un montaje `/static`. Esos son refinamientos que conocerás en el Capítulo 4; los dos elementos esenciales —`PyFlyApplication(LumenApplication)` para arrancar y `create_app(...)` para construir la aplicación web— son exactamente lo que ves aquí.

### Configuración

PyFly lee `pyfly.yaml` desde la raíz del proyecto. Crea uno que nombre la aplicación, establezca el puerto HTTP y active los niveles que necesitamos. Todo está anidado bajo una clave de nivel superior `pyfly`.

::: listing pyfly.yaml | Listado 0.3 — pyfly.yaml
pyfly:
  app:
    name: lumen
    version: 1.0.0
  server:
    # App on 8080; the actuator + admin default to the management port 9090.
    port: 8080
  cqrs:
    enabled: true
  transactional:
    enabled: true
  eda:
    provider: memory          # in-memory event bus, no broker needed
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create        # create tables on startup
:::

Vale la pena dedicar un momento ahora —y un capítulo más adelante— a unas pocas claves. `pyfly.server.port` es el puerto HTTP de la aplicación —`8080` por defecto, exactamente como el `server.port` de Spring—. `data.relational` apunta a un archivo SQLite (`lumen.db`) y `ddl-auto: create` le dice al framework que cree el esquema de la base de datos al arrancar, así que no hay ningún paso de migración que ejecutar para este recorrido. `eda.provider: memory` nos da un bus de eventos en proceso.

!!! warning "Advertencia"
    Si vienes de un PyFly más antiguo, ten en cuenta que la clave del puerto es `pyfly.server.port` (sobreescritura por entorno `PYFLY_SERVER_PORT`). Las antiguas `pyfly.web.port` / `PYFLY_WEB_PORT` se eliminaron: a partir de ahora establece el puerto bajo `pyfly.server`.

### Ejecútalo

Aun sin endpoints todavía, la aplicación arranca. Iníciala:

```bash
uv run pyfly run --server uvicorn
```

Verás el banner de PyFly, registros de arranque estructurados y una línea que te dice que el servidor está escuchando en `http://0.0.0.0:8080`. La opción `--server uvicorn` selecciona el servidor Uvicorn (viene con `pyfly[web]`); para desarrollo, añade `--reload` para reiniciar automáticamente cuando edites un archivo.

PyFly también expone un **endpoint de salud** para que los orquestadores puedan saber que la aplicación está viva. Los endpoints del actuator y el panel de administración se ejecutan en un *puerto de gestión separado*, `9090` por defecto, lo que mantiene los endpoints operativos fuera de tu puerto público de aplicación. En otra terminal:

```bash
curl -s localhost:9090/actuator/health
```

```json
{"status":"UP"}
```

!!! note "Dos puertos, a propósito"
    La aplicación sirve tu API en `8080`; el **puerto de gestión** `9090` sirve `/actuator/health`, `/actuator/info` y el panel de administración. Este es el comportamiento de `management.server.port` de Spring Boot. El puerto de gestión está *abierto y sin autenticación por defecto* —está bien en una red privada, pero en producción establecerías `pyfly.management.security.enabled: true` para protegerlo, o `pyfly.management.server.port: -1` para deshabilitar por completo los endpoints de gestión—. Por defecto, solo `health` e `info` se exponen por HTTP; expón más (métricas, entorno, …) con `pyfly.management.endpoints.web.exposure.include`.

Detén el servidor con `Ctrl-C`. El shell está vacío, pero los cimientos están vivos: el contenedor de inyección de dependencias se construye, el servidor arranca y el informe de salud funciona. Ahora le damos algo que hacer.

---

## Paso 3 — La primera porción del dominio

Empezamos donde DDD dice que hay que empezar: por el modelo. Dos objetos cargan con todo el dominio: `Money`, un objeto de valor para importes exactos, y `Wallet`, el agregado que posee el saldo.

### Money — un objeto de valor

`Money` es el *objeto de valor* de manual: no tiene identidad, dos instancias con el mismo importe y moneda son intercambiables, y nunca cambia. Almacenamos los importes como **unidades menores** enteras (céntimos) más un código de moneda ISO-4217, de modo que la aritmética es exacta: `Money(1050, EUR)` son 10,50 € y no hay redondeo de coma flotante del que preocuparse.

Primero, la diminuta enumeración de moneda de la que depende, bajo `interfaces/enums/v1/`:

::: listing lumen/interfaces/enums/v1/currency.py | Listado 0.4 — La enumeración Currency
from __future__ import annotations

from enum import StrEnum


class Currency(StrEnum):
    """ISO-4217 currency codes Lumen wallets can hold."""

    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"
:::

Ahora `Money` en sí, bajo `models/entities/v1/`. Se construye sobre `pyfly.domain.ValueObject` y es una dataclass congelada, así que la igualdad es estructural y las instancias son inmutables. La aritmética devuelve objetos `Money` *nuevos* y se niega a mezclar monedas.

::: listing lumen/models/entities/v1/money.py | Listado 0.5 — El objeto de valor Money
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.domain import BusinessRuleViolation, ValueObject


@dataclass(frozen=True)
class Money(ValueObject):
    """An exact monetary amount in a single currency (minor units)."""

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise BusinessRuleViolation(
                "money-amount-integer", "amount must be an integer number of minor units"
            )

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        """The additive identity for *currency* (a zero balance)."""
        return cls(amount=0, currency=currency)

    def add(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def subtract(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    @property
    def major_units(self) -> float:
        """The amount as a major-unit decimal (cents / 100)."""
        return round(self.amount / 100, 2)

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise BusinessRuleViolation(
                "money-currency-mismatch",
                f"cannot combine {self.currency.value} with {other.currency.value}",
            )
:::

`BusinessRuleViolation` es la señal del framework de que se ha quebrantado una regla de dominio —aquí, "los importes son unidades menores enteras" y "no puedes sumar euros a dólares"—. Fíjate en que no hay HTTP, ni base de datos, ni cableado del framework: un objeto de valor es dominio puro.

### Wallet — el agregado

El `Wallet` es la *raíz de agregado*: el objeto que posee la invariante. El estado solo cambia a través de métodos que revelan la intención (`open`, `deposit`, `withdraw`), cada uno de los cuales protege la regla **el saldo nunca se vuelve negativo** y registra un *evento de dominio* que describe lo que ocurrió. Construido sobre `pyfly.domain.AggregateRoot`, lanza eventos con `raise_event(...)`, que drenaremos y publicaremos en el Paso 9.

::: listing lumen/models/entities/v1/wallet_entity.py | Listado 0.6 — El agregado Wallet y sus eventos de dominio
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent


@dataclass(frozen=True)
class WalletOpened(DomainEvent):
    wallet_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass(frozen=True)
class FundsDeposited(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


class Wallet(AggregateRoot[str]):
    """Wallet aggregate root — owns the ``balance >= 0`` invariant."""

    __slots__ = ("owner_id", "balance", "created_at")

    def __init__(
        self,
        id: str,
        owner_id: str,
        balance: Money,
        created_at: datetime | None = None,
    ) -> None:
        super().__init__(id)
        self.owner_id = owner_id
        self.balance = balance
        self.created_at = created_at or datetime.now(UTC)

    @property
    def currency(self) -> Currency:
        return self.balance.currency

    @classmethod
    def open(cls, wallet_id: str, owner_id: str, currency: Currency) -> Wallet:
        """Open a new, empty wallet; raises :class:`WalletOpened`."""
        if not owner_id.strip():
            raise BusinessRuleViolation("wallet-owner-required", "owner_id is required")
        wallet = cls(id=wallet_id, owner_id=owner_id, balance=Money.zero(currency))
        wallet.raise_event(
            WalletOpened(wallet_id=wallet_id, owner_id=owner_id, currency=currency.value)
        )
        return wallet

    def deposit(self, amount: Money) -> None:
        """Credit *amount* to the balance; raises :class:`FundsDeposited`."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation("wallet-deposit-positive", "deposit amount must be > 0")
        self.balance = self.balance.add(amount)
        assert self.id is not None
        self.raise_event(
            FundsDeposited(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.balance.currency:
            raise BusinessRuleViolation(
                "wallet-currency-mismatch",
                f"wallet holds {self.balance.currency.value}, got {amount.currency.value}",
            )
:::

::: figure art/figures/06-aggregate.svg | Figura 0.1 — El agregado Wallet posee su invariante; todos los cambios de estado pasan por sus métodos.

!!! note "Nota"
    El `Wallet` terminado en `samples/lumen` también tiene un método `withdraw` y un evento `FundsWithdrawn`, que siguen la misma forma —los hemos dejado fuera aquí para que este primer listado sea corto—. El Capítulo 6 construye el agregado completo.

### Ejecútalo

No necesitas el servidor en ejecución para ejercitar el dominio. Abre un REPL de Python dentro del proyecto y maneja el modelo directamente:

```bash
uv run python
```

```python
>>> from lumen.interfaces.enums.v1.currency import Currency
>>> from lumen.models.entities.v1.money import Money
>>> from lumen.models.entities.v1.wallet_entity import Wallet
>>> w = Wallet.open("wlt-1", "alice", Currency.EUR)
>>> w.deposit(Money(1500, Currency.EUR))
>>> w.balance.amount, w.balance.currency.value
(1500, 'EUR')
>>> w.deposit(Money(100, Currency.USD))     # wrong currency → rejected
pyfly.domain.exceptions.BusinessRuleViolation: wallet holds EUR, got USD
```

El agregado impone sus propias reglas en Python puro, sin necesidad de infraestructura.

---

## Paso 4 — Persístelo

Un monedero que vive solo en memoria no sirve de mucho. Necesitamos guardarlo. La capa de datos de PyFly te da un *repositorio al estilo de Spring Data* sobre SQLAlchemy, y como configuramos SQLite no hay ninguna base de datos que instalar.

### La fila de persistencia

El agregado es rico; la fila en la que se persiste es plana. Mapeamos `Wallet` a un `WalletEntity` —una fila por monedero, con el saldo dividido en una columna entera (`balance_minor`) y una columna de moneda—. Hereda la `Base` declarativa del framework, lo que le permite conservar como clave primaria el propio id de cadena del agregado.

::: listing lumen/models/entities/v1/wallet_orm.py | Listado 0.7 — La fila de persistencia WalletEntity
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's own string id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
:::

Como la clase es subclase de `Base`, importarla registra la tabla `wallets`; con `ddl-auto: create` el framework crea esa tabla al arrancar.

### El repositorio

En lugar de escribir SQL a mano, creas una subclase del `Repository[Entity, IdType]` genérico del framework. Esa única declaración le dice al framework el tipo de la entidad y el tipo de la clave primaria, y a cambio obtienes gratis toda la superficie del repositorio asíncrono: `save`, `find_by_id`, `find_all`, `count`, `delete`, paginación y más, con la sesión de base de datos inyectada por ti.

::: listing lumen/models/repositories/wallet_repository.py | Listado 0.8 — Un repositorio al estilo de Spring Data
from __future__ import annotations

from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Repository


@repository
class WalletRepository(Repository[WalletEntity, str]):
    """CRUD for :class:`WalletEntity`, plus a convenience upsert."""

    async def find_by_owner_id(self, owner_id: str) -> list[WalletEntity]:
        """All wallets owned by *owner_id* (derived query stub)."""
        ...

    async def upsert(self, entity: WalletEntity) -> WalletEntity:
        """Insert *entity*, or update the row with the same id."""
        session = self._require_session()
        merged = await session.merge(entity)
        await session.flush()
        return merged
:::

Dos cosas aquí son Spring Data puro. `find_by_owner_id` es una **consulta derivada**: su cuerpo es un esbozo elidido (`...`), y al arrancar el framework analiza el *nombre* del método y compila por ti un `SELECT … WHERE owner_id = :owner_id` real. `upsert` es una pequeña comodidad sobre `session.merge` para que un manejador pueda persistir un monedero tanto si es nuevo como si ya existe, con una sola llamada.

El decorador `@repository` registra la clase como un componente gestionado —un *bean* en el contenedor de inyección de dependencias— para que pueda inyectarse en los manejadores que escribimos a continuación.

::: figure art/figures/05-repository.svg | Figura 0.2 — Un Repository del framework convierte una declaración tipada en una superficie CRUD completa.

!!! spring "Equivalencia con Spring"
    `Repository[WalletEntity, str]` es el análogo directo del `JpaRepository<WalletEntity, String>` de Spring Data. Tú declaras la interfaz; el framework proporciona la implementación. Las consultas derivadas (`findByOwnerId` en Spring, `find_by_owner_id` aquí) se analizan a partir del nombre del método exactamente de la misma manera.

---

## Paso 5 — Un camino de escritura con CQRS

Ahora cableamos el modelo al repositorio a través de un *comando*. PyFly usa **CQRS** —Command Query Responsibility Segregation (segregación de responsabilidad entre comandos y consultas)—, lo que significa que las escrituras fluyen por un camino (los comandos) y las lecturas por otro (las consultas). Un comando es un objeto pequeño e inmutable que describe la intención; un manejador (handler) lo ejecuta.

### El comando

`OpenWallet` lleva los datos necesarios para abrir un monedero y se valida a sí mismo antes de que nada se ejecute. Es una dataclass congelada que extiende `Command[str]` —el `str` dice "este comando, cuando se maneja, produce un id de monedero"—.

::: listing lumen/core/services/wallets/open_wallet_command.py | Listado 0.9 — El comando OpenWallet
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class OpenWallet(Command[str]):
    """Open a new wallet. Returns the generated wallet id."""

    owner_id: str
    currency: Currency

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.owner_id.strip():
            return ValidationResult.failure("owner_id", "Owner id is required")
        return ValidationResult.success()
:::

### El manejador

El manejador es donde ocurre el trabajo: generar un id, crear el agregado `Wallet`, persistirlo a través del repositorio y luego drenar y publicar los eventos del agregado. Se ejecuta dentro de `@transactional()`, que abre una unidad de trabajo, confirma en caso de éxito y revierte en caso de fallo. El repositorio y el publicador de eventos los inyecta el contenedor —tú solo los declaras en `__init__`—.

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listado 0.10 — El manejador de OpenWallet
from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

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

    @transactional()
    async def do_handle(self, command: OpenWallet) -> str:  # type: ignore[override]
        wallet_id = f"wlt-{uuid4()}"
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
:::

El manejador llama a `to_entity(wallet)` —un pequeño mapeador que aplana el agregado en una fila `WalletEntity`—. Créalo bajo `core/mappers/`. (Añadiremos la proyección del lado de lectura que también necesita en el siguiente paso.)

::: listing lumen/core/mappers/wallet_mapper.py | Listado 0.11 — Mapeando el agregado a su fila
from __future__ import annotations

from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.entities.v1.wallet_orm import WalletEntity


def to_entity(wallet: Wallet) -> WalletEntity:
    """Flatten a :class:`Wallet` aggregate into a persistable row."""
    assert wallet.id is not None
    return WalletEntity(
        id=wallet.id,
        owner_id=wallet.owner_id,
        currency=wallet.currency.value,
        balance_minor=wallet.balance.amount,
        created_at=wallet.created_at,
    )
:::

::: figure art/figures/07-cqrs.svg | Figura 0.3 — Un comando fluye por el bus hasta su manejador; las consultas toman un camino separado.

!!! spring "Equivalencia con Spring"
    `@command_handler` + `@service` registra un manejador al que el bus de comandos despacha —muy parecido a un `@Service` de Spring cuyo método maneja una petición—. `@transactional()` es la contraparte en PyFly del `@Transactional` de Spring: gestiona la unidad de trabajo para que la persistencia se confirme por completo o se revierta por completo.

---

## Paso 6 — Un camino de lectura

Las lecturas toman el otro carril. Una *consulta* hace una pregunta; un *manejador de consultas* la responde, normalmente proyectando una fila de base de datos sobre un DTO pequeño y hecho a propósito. Leeremos solo el saldo.

### El DTO y la consulta

La respuesta del saldo es un diminuto modelo Pydantic bajo `interfaces/dtos/v1/`:

::: listing lumen/interfaces/dtos/v1/balance_dto.py | Listado 0.12 — El modelo de respuesta BalanceDto
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

La consulta lleva solo el id del monedero y declara que devuelve un `BalanceDto` o `None`:

::: listing lumen/core/services/wallets/get_balance_query.py | Listado 0.13 — La consulta GetBalance
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetBalance(Query[BalanceDto | None]):
    """Look up just the balance of a wallet by its identifier."""

    wallet_id: str
:::

### El manejador y la proyección

Añade el mapeador del lado de lectura a `wallet_mapper.py` —una pequeña función que proyecta una fila sobre el DTO, calculando el saldo en unidades mayores—:

::: listing lumen/core/mappers/wallet_mapper.py | Listado 0.14 — Proyectando una fila sobre el DTO de saldo
from __future__ import annotations

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.wallet_orm import WalletEntity


def entity_to_balance_dto(entity: WalletEntity) -> BalanceDto:
    """Project a persisted row onto the lightweight balance DTO."""
    return BalanceDto(
        id=entity.id,
        currency=Currency(entity.currency),
        balance_minor=entity.balance_minor,
        balance=round(entity.balance_minor / 100, 2),
    )
:::

El manejador de la consulta carga la fila por id y la proyecta —devolviendo `None` cuando no existe tal monedero—:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listado 0.15 — El manejador de GetBalance
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetBalance) -> BalanceDto | None:  # type: ignore[override]
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

Fíjate en la asimetría que te da CQRS: el lado de escritura rehidrata el agregado completo para proteger las invariantes; el lado de lectura toca solo las columnas que necesita la vista del saldo y nunca construye un agregado. Cada lado está moldeado para su propio cometido.

---

## Paso 7 — Exponlo por HTTP

El dominio funciona, persiste y tiene caminos de escritura y de lectura. Ahora le ponemos un borde web. Un **controlador** mapea las peticiones HTTP sobre comandos y consultas y las despacha a través del bus —no contiene lógica de negocio propia—.

Primero el DTO de petición para abrir un monedero, bajo `interfaces/dtos/v1/`:

::: listing lumen/interfaces/dtos/v1/open_wallet_request.py | Listado 0.16 — La carga útil OpenWalletRequest
from __future__ import annotations

from pydantic import BaseModel, Field

from lumen.interfaces.enums.v1.currency import Currency


class OpenWalletRequest(BaseModel):
    """Wallet-opening request payload."""

    owner_id: str = Field(min_length=1, max_length=64, description="Identifier of the wallet owner")
    currency: Currency = Field(default=Currency.EUR, description="ISO-4217 currency the wallet holds")
:::

Ahora el controlador, bajo `web/controllers/`. El contenedor de inyección de dependencias inyecta los buses de comandos y de consultas; cada manejador construye un comando o una consulta y lo espera con `await` sobre el bus. Las anotaciones de parámetros declaran de dónde provienen los datos: `Valid[Body[...]]` vincula y valida el cuerpo JSON, `PathVar[str]` vincula un segmento de la URL.

::: listing lumen/web/controllers/wallet_controller.py | Listado 0.17 — El controlador de monederos
from __future__ import annotations

from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import Body, PathVar, Valid, get_mapping, post_mapping, request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open a wallet, read its balance."""

    def __init__(self, commands: DefaultCommandBus, queries: DefaultQueryBus) -> None:
        self._commands = commands
        self._queries = queries

    @post_mapping("", status_code=201)
    async def open_wallet(self, request: Valid[Body[OpenWalletRequest]]) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(owner_id=request.owner_id, currency=request.currency)
        )
        return {"wallet_id": wallet_id}

    @get_mapping("/{wallet_id}/balance")
    async def wallet_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        result = await self._queries.query(GetBalance(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result
:::

::: figure art/figures/04-request.svg | Figura 0.4 — Una petición se vincula a un manejador, que despacha un comando o una consulta a través del bus.

### Ejecútalo

Inicia el servidor:

```bash
uv run pyfly run --server uvicorn
```

En otra terminal, abre un monedero:

```bash
curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"alice","currency":"EUR"}'
```

```json
{"wallet_id":"wlt-7d2c1a9e-..."}
```

Lee el saldo de vuelta (sustituye el id que obtuviste arriba):

```bash
curl -s localhost:8080/api/v1/wallets/wlt-7d2c1a9e-.../balance
```

```json
{"id":"wlt-7d2c1a9e-...","currency":"EUR","balance_minor":0,"balance":0.0}
```

Un monedero recién abierto tiene saldo cero —exactamente lo que `Money.zero(EUR)` produjo allá en la fábrica `open` del agregado—. La petición viajó desde HTTP, a través del bus de comandos, hasta el manejador, a través del repositorio, hasta SQLite, y de vuelta por el camino de lectura. Ese es todo el arco, de extremo a extremo.

!!! tip "Documentación interactiva gratis"
    Mientras el servidor se ejecuta, abre `http://localhost:8080/docs` en un navegador. PyFly generó un documento OpenAPI y una interfaz Swagger UI a partir de tu controlador y tus DTOs —puedes probar los endpoints ahí mismo, sin código adicional—.

---

## Paso 8 — Demuéstralo con una prueba

Ejecutar `curl` a mano está bien una vez; una prueba lo demuestra para siempre. PyFly está diseñado para ser testeable sin un servidor en ejecución —puedes despachar comandos y consultas directamente a través de los buses—. Escribe una prueba bajo `tests/`:

::: listing tests/test_quickstart.py | Listado 0.18 — Una prueba de extremo a extremo a través de los buses
from __future__ import annotations

import pytest
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus


@pytest.mark.asyncio
async def test_open_wallet_then_read_balance(
    command_bus: DefaultCommandBus,
    query_bus: DefaultQueryBus,
) -> None:
    wallet_id = await command_bus.send(OpenWallet(owner_id="alice", currency=Currency.EUR))
    assert wallet_id.startswith("wlt-")

    balance = await query_bus.query(GetBalance(wallet_id=wallet_id))
    assert balance is not None
    assert balance.balance_minor == 0
    assert balance.currency is Currency.EUR
:::

Los parámetros `command_bus` y `query_bus` son *fixtures*: arrancan el contexto de aplicación una vez y te entregan buses cableados, los mismos componentes que el controlador usa en producción. (El `samples/lumen/tests/conftest.py` terminado define estas fixtures; cópialo cuando construyas tu propia batería de pruebas —el Capítulo 16 lo explica al completo—.)

### Ejecútalo

```bash
uv run --extra dev pytest -q
```

```
.                                                       [100%]
1 passed in 0.42s
```

Verde. Ahora tienes una funcionalidad de monedero que no solo se ejecuta, sino que está *verificada* —la misma prueba se ejecuta en CI en cada cambio—.

!!! spring "Equivalencia con Spring"
    Despachar a través de los buses en una prueba refleja las pruebas de porción (slice tests) de Spring Boot: ejercitas beans reales cableados sin levantar el servidor HTTP. Las fixtures `command_bus` / `query_bus` son el equivalente en PyFly de un `ApplicationContext` de Spring inyectado en un `@SpringBootTest`.

---

## Paso 9 — Una probada de eventos

El agregado ha estado registrando eventos de dominio todo este tiempo —`WalletOpened`, `FundsDeposited`— y el manejador los drena con `wallet.clear_events()` y los publica. Hasta ahora nada ha *escuchado*. Añadamos un pequeño oyente que reaccione.

Primero, el puente de publicación que el manejador ya importa. Convierte cada evento de dominio drenado en una carga útil y lo publica en el bus de eventos, bajo `core/services/wallets/`:

::: listing lumen/core/services/wallets/event_publishing.py | Listado 0.19 — Publicando los eventos de dominio drenados
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from lumen.core.services.listeners.wallet_audit_listener import WALLET_EVENTS_DESTINATION
from pyfly.domain import DomainEvent
from pyfly.eda import EventPublisher


def _to_payload(event: DomainEvent) -> dict[str, Any]:
    """Flatten a frozen-dataclass domain event into a JSON-friendly dict."""
    payload: dict[str, Any] = dataclasses.asdict(event)
    payload.setdefault("event_type", event.event_type)
    return payload


async def publish_domain_events(publisher: EventPublisher, events: Iterable[DomainEvent]) -> None:
    """Publish each drained domain event on the wallet events channel."""
    for event in events:
        await publisher.publish(
            destination=WALLET_EVENTS_DESTINATION,
            event_type=event.event_type,
            payload=_to_payload(event),
        )
:::

Ahora el oyente, bajo `core/services/listeners/`. Es un simple `@service` cuyo método está marcado con `@event_listener`; al arrancar, PyFly lo descubre y lo suscribe al bus —sin cableado a mano—. Aquí mantiene un diminuto registro de auditoría en memoria y un total acumulado por monedero.

::: listing lumen/core/services/listeners/wallet_audit_listener.py | Listado 0.20 — Un oyente de eventos de dominio
from __future__ import annotations

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener

# The logical channel the wallet handlers publish domain events to.
WALLET_EVENTS_DESTINATION = "wallet.events"


@service
class WalletAuditListener:
    """In-memory audit log + running-total projection over wallet events."""

    def __init__(self) -> None:
        self._running_totals: dict[str, int] = {}

    @event_listener(event_types=["WalletOpened", "FundsDeposited"])
    async def on_wallet_event(self, envelope: EventEnvelope) -> None:
        payload = dict(envelope.payload)
        wallet_id = str(payload.get("wallet_id", ""))
        if envelope.event_type == "WalletOpened":
            self._running_totals.setdefault(wallet_id, 0)
        elif envelope.event_type == "FundsDeposited":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = self._running_totals.get(wallet_id, 0) + amount

    def running_total(self, wallet_id: str) -> int:
        """Net funds for *wallet_id*, in minor units."""
        return self._running_totals.get(wallet_id, 0)
:::

Añade el paquete de oyentes a `scan_packages` en `app.py` para que el contenedor lo descubra:

```python
scan_packages=[
    "lumen.models.repositories",
    "lumen.core.services.wallets",
    "lumen.core.services.listeners",   # <-- add this
    "lumen.web.controllers",
],
```

::: figure art/figures/08-eda.svg | Figura 0.5 — Un manejador publica eventos de dominio; los oyentes se suscriben y reaccionan, desacoplados del comando.

### Ejecútalo

Abre un monedero, luego haz un depósito, y el total acumulado del oyente se actualiza como efecto secundario de esos comandos —sin que el comando sepa que el oyente existe—. Ese desacoplamiento es el sentido mismo de los eventos: añades reacciones (registros de auditoría, notificaciones, proyecciones) sin tocar el código que las disparó.

!!! spring "Equivalencia con Spring"
    `@event_listener` es la contraparte en PyFly del `@EventListener` de Spring. Publicar a través de un `EventPublisher` y suscribirse con un método marcado es el mismo modelo de publicación/suscripción que el `ApplicationEventPublisher` de Spring y los beans anotados con `@EventListener`.

---

## Lo que construiste {.recap}

Acabas de construir —y probar— una porción vertical real de un servicio: un modelo de dominio, una base de datos, un camino de escritura, un camino de lectura, un borde HTTP y una reacción a eventos. Cada una de esas cosas fue un *adelanto*. El resto del libro desarma cada una y la reconstruye como es debido, con el razonamiento, las alternativas y los detalles de producción.

Aquí tienes el mapa de lo que acabas de hacer al capítulo que profundiza:

| En este Inicio rápido… | Se profundiza en |
|---|---|
| Viste el contenedor construir e inyectar tus beans (`@repository`, `@service`) | **Capítulo 2** — Inyección de dependencias y el contexto de aplicación |
| Configuraste la aplicación con `pyfly.yaml` y el puerto de gestión | **Capítulo 3** — Configuración, perfiles y secretos |
| Expusiste una API HTTP con `@rest_controller`, vinculación y validación | **Capítulo 4** — Tu primera API HTTP |
| Persististe con un `Repository` del framework sobre SQLAlchemy/SQLite | **Capítulo 5** — Persistencia y el patrón Repositorio |
| Modelaste `Money`, `Wallet`, invariantes y eventos de dominio | **Capítulo 6** — Diseño orientado al dominio |
| Separaste escrituras y lecturas con comandos, consultas y el bus | **Capítulo 7** — CQRS: comandos y consultas |
| Publicaste y reaccionaste a eventos de dominio con `@event_listener` | **Capítulo 8** — Eventos de dominio y arquitectura orientada a eventos |
| (Próximamente) reconstruiste el estado a partir de un registro de eventos | **Capítulo 9** — Event sourcing del libro mayor |
| (Próximamente) llamaste a otros servicios y dividiste el monolito | **Capítulos 11–12** — Clientes HTTP, el BFF y las sagas |
| (Próximamente) lo aseguraste, observaste y desplegaste | **Capítulos 14–18** — Seguridad, observabilidad, pruebas y producción |

Cuando estés listo para el *porqué* de todo esto, pasa la página al Capítulo 1.

---

## Pruébalo tú mismo {.exercises}

Si quieres seguir avanzando por tu cuenta primero, tres pequeñas extensiones se construyen directamente sobre lo que ya tienes:

1. **Añade un endpoint de depósito.** Ya tienes el evento `FundsDeposited` y el método `deposit` del agregado. Añade un comando `DepositFunds` + manejador (modélalos sobre `OpenWallet`), una ruta `POST /{wallet_id}/deposit`, y observa cómo trepa el total acumulado del oyente.
2. **Añade un camino de `withdraw`** que se niegue a sobregirar —la invariante `balance >= 0` del agregado debería rechazarlo, y tu manejador debería exponerlo como un error limpio—. Añade un evento de dominio `FundsWithdrawn` que refleje el evento `FundsDeposited` mostrado en el Listado 0.6.
3. **Escribe una prueba para el oyente**, comprobando que abrir un monedero y depositar deja el total acumulado esperado —demostrando el camino de eventos de extremo a extremo—.
