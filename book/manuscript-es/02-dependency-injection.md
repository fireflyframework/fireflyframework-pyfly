<span class="eyebrow">Capítulo 2</span>

# Inyección de dependencias y el contexto de aplicación {.chtitle}

::: figure art/openers/ch02.svg | &nbsp;

En el capítulo anterior le diste a Lumen su punto de entrada de
aplicación y observaste cómo arrancaba el contenedor. Ahora vas a
declarar los primeros componentes reales de Lumen — un
`WalletRepository` que hereda del `Repository` estilo Spring Data del
framework, un `WalletEntity` que mapea la fila de persistencia y un
manejador CQRS que depende de ambos — y dejarás que PyFly los conecte
entre sí a partir de nada más que anotaciones de tipos.
Sin factorías, sin `new` manual, sin código de pegamento.

Este capítulo es práctico. Construiremos cada pieza en pasos
pequeños y numerados, y después de cada hito hay un punto de control
**Ejecútalo** que muestra el comando exacto que debes escribir y la
salida que deberías ver en pantalla. Si tu salida coincide, vas por
buen camino; si no, la diferencia te indica con precisión qué
corregir. Vas siguiendo el ejercicio dentro de `samples/lumen` con
PyFly **v26.6.110** instalado (`uv sync` desde el directorio de Lumen
lo descarga).

!!! note "Nuevo término: contenedor"
    A lo largo del capítulo hablamos de *el contenedor*. Un
    **contenedor** es simplemente el objeto que PyFly crea al arrancar
    y que sabe cómo construir, conectar y entregar todos los objetos de
    tu aplicación. Nunca lo construyes tú mismo — cuando ejecutas la
    aplicación, PyFly levanta el contenedor, lo llena con tus beans y
    lo mantiene vivo hasta el apagado. Piénsalo como un almacén
    inteligente que tanto guarda tus objetos como los ensambla bajo
    demanda.

Antes de que aparezca una sola línea de código de Lumen, conviene
detenerse en *por qué* eso importa. En un proyecto Python
convencional escribirías algo como:

```python
handler = DepositFundsHandler(
    repository=InMemoryWalletRepository(),
    events=InMemoryEventBus(),
)
```

en algún punto cercano a la ruta de arranque. Esa línea parece
inofensiva, pero fija cada decisión — qué clase de repositorio, qué
bus de eventos — en el punto de construcción. Cambia el repositorio
por un adaptador de Postgres y tendrás que encontrar cada lugar de
construcción. Añade un doble de prueba y necesitarás reestructurar el
cableado. La **inyección de dependencias** invierte esta relación: las
clases *declaran* lo que necesitan, y el contenedor *decide* qué
proporcionar. El resultado es código abierto a la extensión pero
cerrado a la modificación — el `DepositFundsHandler` que escribes hoy
aceptará un adaptador de base de datos de producción en la Parte II
sin un solo cambio en su código fuente.

---

## Estereotipos: declarar tus beans

Antes de que el contenedor pueda conectar nada, necesita saber qué
clases gestionar. Un **bean** es cualquier objeto que el contenedor
crea, conecta y posee. Haces que una clase sea visible para el
contenedor aplicando un **decorador de estereotipo** — una anotación
ligera que registra la clase y señala su rol arquitectónico.

!!! note "Nuevos términos: bean y estereotipo"
    Un **bean** no es más que una instancia que el contenedor posee —
    la construye, le suministra sus dependencias y (normalmente)
    mantiene una única copia compartida. Un **estereotipo** es el
    decorador que pones en una clase para decir "contenedor, esto es
    tuyo, por favor gestiónalo". La palabra *estereotipo* está tomada
    de Spring; simplemente significa "un rol etiquetado". Poner
    `@service` en una clase es todo el acto de registro — no hay un
    fichero de configuración aparte que editar.

PyFly incluye cinco estereotipos:

| Decorador | Significado |
|---|---|
| `@service` | Capa de lógica de negocio: operaciones de dominio, orquestación de casos de uso. |
| `@component` | Bean gestionado genérico sin un rol arquitectónico específico. |
| `@repository` | Capa de acceso a datos: bases de datos, almacenamiento externo, puertos. |
| `@configuration` | Clase de configuración que puede contener métodos factoría `@bean`. |
| `@rest_controller` | Capa HTTP: gestiona peticiones y devuelve respuestas JSON. |

Los cinco estereotipos son **equivalentes a nivel de contenedor**:
comparten la misma factoría interna `_make_stereotype()` y aceptan los
mismos argumentos de palabra clave opcionales (`name`, `scope`,
`profile`, `condition`). Las diferencias significativas son la
etiqueta `__pyfly_stereotype__` — usada por la capa web para descubrir
controladores y por el contexto para encontrar clases
`@configuration` — y la claridad arquitectónica que cada nombre aporta
a quienes leen tu código. Elegir `@repository` en lugar de
`@component` no cuesta nada técnicamente, pero le dice a todo futuro
lector exactamente para qué sirve la clase.

Funcionan tanto la forma simple como la forma con paréntesis:

```python
@service              # bare — all defaults
class SimpleService:
    pass

@service(name="wallet_svc")   # with keyword args
class NamedService:
    pass
```

### El arranque con scan_packages

El contenedor solo descubre beans en los paquetes que se le ha
indicado escanear. En `lumen/app.py`, `@pyfly_application` lista cada
subpaquete que el contenedor debe inspeccionar en busca de
declaraciones de estereotipo:

::: listing lumen/app.py | Listado 2.1 — Punto de entrada de la aplicación con scan_packages
from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack


@enable_domain_stack
@pyfly_application(
    name="lumen",
    version="1.0.0",
    description=(
        "Lumen — a DDD digital-wallet service"
        " built on the PyFly framework."
    ),
    scan_packages=[
        "lumen.models.repositories",
        "lumen.core.services.wallets",
        "lumen.core.services.transfers",
        "lumen.core.services.listeners",
        "lumen.web.controllers",
    ],
)
class LumenApplication:
    pass
:::

**Cómo funciona.** `@pyfly_application` registra `LumenApplication`
como la raíz de la aplicación y siembra el contenedor con las
autoconfiguraciones del framework. `scan_packages` es la lista exacta
de rutas de paquetes Python que el contenedor recorre al arrancar,
recolectando cada clase decorada con un estereotipo. Cualquier paquete
que no esté listado aquí es invisible para el contenedor — la fuente
más habitual de confusión del tipo "¿por qué no se encuentra mi bean?"
al añadir nuevos subpaquetes. `@enable_domain_stack` activa en una sola
línea las autoconfiguraciones de CQRS, el motor transaccional, event
sourcing, datos relacionales y el motor de reglas.

Lee ese listado como cuatro decisiones:

- **Paso 1 — nombrar la aplicación.** `name="lumen"` y
  `version="1.0.0"` se convierten en la identidad que reportan el
  banner de arranque y el endpoint `/actuator/info`. (Estos son el
  nombre y la versión de la *aplicación*; la versión del framework es
  aparte — aquí, v26.6.110.)
- **Paso 2 — describirla.** `description=...` son metadatos orientados
  a personas que se muestran en la documentación de API generada.
- **Paso 3 — listar los paquetes a escanear.** Cada entrada de
  `scan_packages` es una ruta Python con puntos que el contenedor
  importará e inspeccionará. Si una clase con un estereotipo vive en un
  paquete que *no* está en esta lista, el contenedor nunca la verá.
- **Paso 4 — habilitar el stack.** `@enable_domain_stack` enciende las
  autoconfiguraciones de la capa de dominio para que los buses de
  CQRS, la sesión transaccional y la capa de datos relacional existan
  todos antes de que tus beans se conecten.

!!! tip "Consejo: escanea el paquete, no la clase"
    Las entradas de `scan_packages` son rutas de *paquete*
    (`lumen.web.controllers`), nunca rutas de módulo o clase
    individuales. El contenedor recorre el paquete y descubre cada
    clase decorada con un estereotipo dentro de él. Cuando añades un
    nuevo manejador bajo `lumen.core.services.wallets`, se recoge
    automáticamente — sin necesidad de editar `scan_packages`. Solo
    tocas esta lista cuando introduces un subpaquete completamente
    nuevo.

**Ejecútalo.** Desde la raíz del proyecto Lumen, arranca la aplicación
y observa cómo el contenedor se ensambla a sí mismo:

```bash
cd samples/lumen
uv run pyfly run --server uvicorn
```

Deberías ver el banner seguido de líneas estructuradas de arranque —
el contenedor reportando exactamente lo que escaneó y conectó:

```text
:: PyFly Framework :: (v26.6.110) (Python 3.12.13)

pyfly.core: starting_application | app=lumen version=1.0.0
pyfly.core: scanned_package | package=lumen.models.repositories beans_found=1
pyfly.core: scanned_package | package=lumen.core.services.wallets beans_found=7
pyfly.core: scanned_package | package=lumen.core.services.transfers beans_found=2
pyfly.core: scanned_package | package=lumen.core.services.listeners beans_found=1
pyfly.core: scanned_package | package=lumen.web.controllers beans_found=1
pyfly.core: bean_summary | total=137 services=10 repositories=1 controllers=4 configurations=19
pyfly.core: server_started | server=uvicorn host=0.0.0.0 port=8080 workers=1
pyfly.core: application_started | app=lumen startup_time_s=0.143 beans_initialized=137
```

Las líneas `scanned_package` son `scan_packages` haciendo su trabajo:
una línea por entrada, cada una reportando cuántos beans encontró. La
línea final `application_started` — el equivalente del "Started
Application in N seconds" de Spring Boot — es tu señal de que el
contexto arrancó limpiamente. Pulsa `Ctrl-C` para detener el servidor.

!!! note "Nuevo término: el puerto de gestión"
    Aparecen dos puertos al arrancar. La API HTTP de tu aplicación
    escucha en `pyfly.server.port` (por defecto **8080**). Los
    endpoints operativos — la comprobación de salud, info y el panel de
    administración — escuchan por separado en el **puerto de gestión**
    `pyfly.management.server.port` (por defecto **9090**), que está
    abierto y sin autenticación por defecto. No tocarás ninguno de los
    dos en este capítulo, pero vale la pena saber por qué se encienden
    dos puertos. (La clave antigua `pyfly.web.port` se eliminó en
    v26.6.102; usa siempre `pyfly.server.port` ahora.)

**Qué acaba de pasar.** Un solo comando hizo mucho. PyFly cargó
`pyfly.yaml`, importó cada paquete de `scan_packages`, encontró cada
clase decorada con un estereotipo, le pidió al contenedor que las
construyera en orden de dependencias y reportó los totales — 137
beans, de los cuales la mayoría son beans de autoconfiguración del
framework y solo un puñado son tuyos por ahora. A partir de aquí, el
resto del capítulo trata de *añadir* a ese recuento de beans: una
entidad, un repositorio y un manejador de comandos, cada uno
descubierto exactamente por este mecanismo.

!!! spring "Equivalencia con Spring"
    `scan_packages` es el equivalente de `@ComponentScan(basePackages =
    {...})` de Spring. La semántica es idéntica: lista cada subpaquete
    que quieras que el framework inspeccione, y registrará todo lo que
    encuentre. La línea de log `application_started` refleja el resumen
    de arranque "Started Application in N seconds (process running for
    M)" de Spring Boot.

### La entidad y el repositorio

Lumen almacena los monederos en una base de datos relacional. Dos
clases asumen esta responsabilidad: `WalletEntity` (la fila de
persistencia) y `WalletRepository` (el bean de acceso a datos). Las
construiremos en orden: primero la forma de la fila, luego el bean de
acceso a datos que la lee y la escribe.

!!! note "Nuevo término: entidad"
    Una **entidad** es la forma en base de datos de un registro — aquí,
    un monedero, almacenado como una fila en una tabla `wallets`. Es
    deliberadamente simple: solo columnas tipadas, sin comportamiento.
    (Lumen mantiene aparte el objeto de dominio *rico*, la raíz de
    agregado `Wallet`; lo conocerás en el Capítulo 6. Por ahora, la
    entidad es simplemente la forma en que un monedero se escribe en la
    base de datos y se lee de ella.)

**La entidad.** `WalletEntity` es una clase mapeada con SQLAlchemy que
hereda el `Base` del framework. Constrúyela campo a campo:

- **Paso 1 — heredar `Base`.** Heredar del `Base` declarativo de PyFly
  es lo que inscribe la clase en los metadatos del ORM para que el
  framework pueda crear su tabla.
- **Paso 2 — nombrar la tabla.** `__tablename__ = "wallets"` es la
  tabla SQL a la que esta clase mapea.
- **Paso 3 — declarar la clave primaria.** `id` es una columna `str`
  marcada `primary_key=True` — el monedero conserva su propio id de
  dominio (`wlt-…`) en lugar de un número generado.
- **Paso 4 — declarar las columnas restantes.** `owner_id`,
  `currency`, `balance_minor` (el saldo en unidades menores —
  céntimos — de modo que nunca haya un error de redondeo de coma
  flotante) y una marca de tiempo `created_at`.

::: listing lumen/models/entities/v1/wallet_orm.py | Listado 2.2a — WalletEntity: la fila de persistencia
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's own id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(
        nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC)
    )
:::

Heredar `Base` (la base declarativa de PyFly) registra la tabla
`wallets` en `Base.metadata`; el ciclo de vida del motor del framework
la crea al arrancar. No se necesita más cableado.

**Ejecútalo.** Con `ddl-auto: create` establecido en `pyfly.yaml`, el
framework construye el esquema a partir de tus entidades mapeadas en el
momento en que arranca la aplicación. Vuelve a arrancar la aplicación y
busca las líneas de la capa de datos:

```bash
uv run pyfly run --server uvicorn
```

```text
pyfly.data.relational.auto_configuration: Initializing database schema (ddl-auto=create)
pyfly.data.relational.auto_configuration: Database schema initialized (1 tables)
```

`1 tables` es tu tabla `wallets` — creada puramente porque
`WalletEntity` hereda `Base`. No hay script de migración que ejecutar
ni `CREATE TABLE` que escribir a mano. (Lumen usa SQLite por defecto,
así que la base de datos no es más que un fichero `lumen.db` en el
directorio del proyecto.)

!!! note "Nuevo término: repositorio"
    Un **repositorio** es el objeto con el que habla tu código cuando
    quiere cargar o guardar entidades. En lugar de escribir SQL, llamas
    a métodos como `find_by_id` o `save`. La clase base `Repository` de
    PyFly *genera* esos métodos por ti a partir de los tipos de la
    entidad y de la clave, así que el repositorio que escribes está
    casi vacío — tú lo declaras, y el framework rellena la
    implementación. (CRUD, usado más abajo, no es más que el acrónimo
    de Create, Read, Update, Delete — las cuatro operaciones básicas de
    datos.)

**El repositorio.** `WalletRepository` hereda del `Repository`
genérico del framework `Repository[WalletEntity, str]`. Los dos
argumentos de tipo le dicen al framework el *tipo de entidad*
(`WalletEntity`) y el *tipo de la clave primaria* (`str`); a partir de
ahí genera e inyecta una superficie CRUD asíncrona completa —
`find_by_id`, `save`, `find_all`, `find_all(pageable)`, `delete`,
`delete_by_id`, `count` y más — respaldada por la `AsyncSession`
transaccional de la autoconfiguración relacional:

::: listing lumen/models/repositories/wallet_repository.py | Listado 2.2b — WalletRepository: subclase del Repository del framework
from __future__ import annotations

from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data import Page, Pageable
from pyfly.data.relational.sqlalchemy import (
    Repository,
    Specification,
)


def balance_at_least(min_minor: int) -> Specification[WalletEntity]:
    """Reusable predicate: wallets with balance >= min_minor."""
    return Specification(
        lambda root, q: q.where(root.balance_minor >= min_minor)
    )


@repository
class WalletRepository(Repository[WalletEntity, str]):
    """CRUD + derived + specification queries for WalletEntity."""

    # Derived query — compiled from the name by the post-processor
    async def find_by_owner_id(
        self, owner_id: str
    ) -> list[WalletEntity]:
        """All wallets owned by owner_id (derived query stub)."""
        ...

    # Specification query — composable predicate + pagination
    async def find_rich(
        self, min_minor: int, pageable: Pageable
    ) -> Page[WalletEntity]:
        """Page of wallets with balance >= min_minor."""
        return await self.find_all_by_spec_paged(
            balance_at_least(min_minor), pageable
        )

    # Upsert: one call for INSERT or UPDATE
    async def upsert(self, entity: WalletEntity) -> WalletEntity:
        """Persist entity whether the row is new or already exists."""
        session = self._require_session()
        merged = await session.merge(entity)
        await session.flush()
        return merged
:::

**Cómo funciona.** `@repository` le dice al contenedor que gestione
`WalletRepository` como un bean de DI. El framework lee
`Repository[WalletEntity, str]` al arrancar, genera internamente la
implementación CRUD y registra la clase — inyectas `WalletRepository`
directamente por tipo en cualquier parte de la aplicación. No hay
interfaz de puerto escrita a mano ni adaptador aparte que mantener:
**el framework suministra e inyecta la implementación; tú dependes de
la propia clase del repositorio por tipo.**

Los tres métodos extra muestran los puntos de extensión que el
framework expone por encima del CRUD heredado. Míralos de uno en uno:

- **Paso 1 — una consulta derivada.** `find_by_owner_id` es una
  **consulta derivada**: el `RepositoryBeanPostProcessor` (un
  componente de arranque que edita los beans después de que se
  construyan) analiza el *nombre* del método y compila un
  `SELECT … WHERE owner_id = :owner_id` real. Tú escribes solo el
  cuerpo vacío (`...`); el framework suministra el SQL. La convención
  de nombres es la API — `find_by_<column>` se convierte en
  `WHERE <column> = ?`.
- **Paso 2 — una consulta con Specification.** `find_rich` compone un
  predicado `Specification` reutilizable (aquí, `balance_at_least`) y
  lo ejecuta con paginación y ordenación mediante el heredado
  `find_all_by_spec_paged`. Las Specifications son la forma de
  construir una cláusula `WHERE` componible y con seguridad de tipos
  cuando un nombre de método resultaría inmanejable.
- **Paso 3 — un upsert.** `upsert` es una conveniencia ligera sobre
  `session.merge` para que un manejador de comandos pueda persistir una
  entidad tanto si es nueva (INSERT) como si ya existe (UPDATE) con una
  sola llamada. Como el monedero posee su propio id, ambos casos se
  basan en la misma clave primaria.

**Qué acaba de pasar.** Declaraste un repositorio cuyo cuerpo está casi
enteramente vacío y, sin embargo, ahora expone una superficie CRUD
asíncrona completa más una consulta derivada, una consulta de
specification y un upsert. El estereotipo `@repository` lo registró
como bean; el framework leyó la base `Repository[WalletEntity, str]`,
generó la implementación y la hizo inyectable por tipo. Tú escribiste
la intención; PyFly escribió la fontanería.

!!! tip "Consejo: confirma que el repositorio está registrado"
    Vuelve a ejecutar `uv run pyfly run --server uvicorn` y mira la
    línea `bean_summary`: `repositories=1`. Ese único repositorio
    registrado es tu `WalletRepository`. Si alguna vez añades un segundo
    repositorio y no aparece en este recuento, la causa habitual es que
    su paquete falta en `scan_packages`.

!!! spring "Equivalencia con Spring"
    `@service`, `@component`, `@repository` y `@configuration` mapean
    directamente a `@Service`, `@Component`, `@Repository` y
    `@Configuration` de Spring. `@rest_controller` refleja
    `@RestController`. `Repository[E, ID]` refleja el
    `JpaRepository<E, ID>` de Spring Data: declara los tipos de entidad
    y de clave; el framework genera e inyecta la implementación
    completa. Los métodos de consulta derivada (con nombres como
    `find_by_owner_id`) se compilan a SQL al arrancar — el mismo
    mecanismo que la derivación de consultas de Spring Data a partir de
    los nombres de los métodos.

---

## Inyección por constructor

Con el repositorio declarado, necesitas un manejador que lo use. Ahí es
donde se hace visible la capacidad más importante del contenedor: nunca
llamas tú mismo a los constructores. Declaras lo que una clase
*necesita* como parámetros de `__init__` con anotaciones de tipo, y el
contenedor los rellena automáticamente. Esto es la **inyección por
constructor**, y es el enfoque recomendado para todas las dependencias
obligatorias.

!!! note "Nuevo término: inyección"
    La **inyección** es el acto del contenedor de *entregar* a una clase
    los objetos de los que depende, en lugar de que la clase los
    construya por sí misma. Con la inyección *por constructor*,
    simplemente listas las dependencias como parámetros tipados de
    `__init__`; el contenedor lee esas anotaciones de tipo y pasa los
    beans correspondientes cuando construye el objeto. La clase nunca
    dice *cómo* obtener sus dependencias — solo *qué* necesita.

El modelo mental es una simple lista de deseos: lista los tipos que
necesitas; el contenedor entrega las instancias correctas. Si una
dependencia no existe al arrancar, obtienes de inmediato un claro
`NoSuchBeanError` — no un críptico `AttributeError` tres marcos de pila
más adentro en tiempo de ejecución.

### Apilar decoradores de manejador sobre @service

En el diseño CQRS de Lumen, cada manejador del lado de escritura lleva
dos decoradores: `@command_handler` (o `@query_handler`) **apilado
sobre `@service`**. El patrón es innegociable: `@service` registra la
clase como un bean; el decorador CQRS añade únicamente metadatos de
enrutado (`__pyfly_command_type__` o `__pyfly_query_type__`) para que
el bus de comandos/consultas pueda despachar al manejador correcto. Sin
`@service`, el contenedor nunca ve la clase y el bus de comandos lanza
`CommandHandlerNotFoundException` en el momento del despacho (el bus de
consultas lanza `QueryHandlerNotFoundException` cuando falta un
manejador de consultas).

Antes de leer el listado, esta es la forma de lo que estás a punto de
escribir, paso a paso:

- **Paso 1 — registrar el bean.** Pon `@service` directamente sobre la
  clase. Esta es la línea que hace que el contenedor lo gestione.
- **Paso 2 — añadir metadatos de enrutado.** Apila `@command_handler`
  *encima* de `@service`. Lee `CommandHandler[DepositFunds, int]` y
  registra "este bean maneja comandos `DepositFunds`".
- **Paso 3 — declarar dependencias en `__init__`.** Lista el
  repositorio, el publicador de eventos y la factoría de sesiones como
  parámetros tipados. Esta única firma es la especificación completa de
  cableado — el contenedor la lee y suministra los tres.
- **Paso 4 — escribir la lógica de negocio en `do_handle`.** Envuélvela
  en `@transactional()` para que toda la secuencia cargar-mutar-guardar
  sea una única unidad de trabajo confirmada.

El `DepositFundsHandler` muestra el patrón completo:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 2.3 — DepositFundsHandler: @command_handler + @service apilados
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import (
    DepositFunds,
)
from lumen.core.services.wallets.event_publishing import (
    publish_domain_events,
)
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns new balance."""

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
    async def do_handle(  # type: ignore[override]
        self, command: DepositFunds
    ) -> int:
        entity = await self._repository.find_by_id(
            command.wallet_id
        )
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(
            Money(amount=command.amount, currency=wallet.currency)
        )
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(
            self._events, wallet.clear_events()
        )
        return wallet.balance.amount
:::

**Cómo funciona.** Cinco decisiones son visibles en este listado:

- `@service` registra la clase como un bean singleton. Sin él, el
  contenedor nunca ve la clase.
- `@command_handler` (aplicado encima de `@service`, así que se ejecuta
  *después* del registro) lee el primer argumento genérico de
  `CommandHandler[DepositFunds, int]` y registra que este bean maneja
  comandos `DepositFunds`.
- La firma de `__init__` es la especificación completa de cableado:
  `repository: WalletRepository` — el bean CRUD generado por el
  framework; `events: EventPublisher` — resuelto por la
  autoconfiguración de CQRS; `session_factory:
  async_sessionmaker[AsyncSession]` — la factoría de conexiones
  compartida proporcionada por la autoconfiguración relacional. Los
  tres se resuelven por tipo; `DepositFundsHandler` nunca importa una
  clase concreta.
- `@transactional()` sobre `do_handle` envuelve todo el cuerpo en una
  única unidad de trabajo confirmada. El decorador abre una sesión
  desde `session_factory`, la vincula al repositorio durante la
  llamada y confirma en caso de éxito (o revierte en caso de error).
- La lógica de negocio sigue la secuencia estándar CQRS/DDD: cargar la
  entidad, rehidratar el agregado mediante el mapeador, mutar a través
  de métodos de dominio que imponen invariantes, persistir mediante
  `upsert`, drenar y publicar los eventos. El monedero se guarda
  *antes* de que se publiquen los eventos, de modo que cualquier
  oyente que consulte el repositorio encuentre el registro
  actualizado.

Un manejador del lado de lectura usa el mismo patrón de apilado, solo
que con `@query_handler` y `QueryHandler`:

```python
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from lumen.models.repositories.wallet_repository import WalletRepository


@query_handler
@service
class GetWalletHandler(QueryHandler[GetWallet, WalletDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(
        self, query: GetWallet
    ) -> WalletDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_dto(entity) if entity is not None else None
```

El contenedor resuelve las dependencias de forma **recursiva**. Cuando
construye `DepositFundsHandler` también construye `WalletRepository`
(el bean CRUD generado por el framework), el `EventPublisher` y el
`async_sessionmaker` — ninguno de los cuales el manejador necesita
conocer.

**Qué acaba de pasar.** Declaraste un manejador que necesita tres
colaboradores y no escribiste ni una línea de código de cableado. El
contenedor leyó las anotaciones de tipo de `__init__`, construyó cada
dependencia (y *sus* dependencias, de forma recursiva) y entregó el
objeto terminado a quien pida `DepositFundsHandler`. Cambiar el bus de
eventos en memoria por Kafka más adelante no tocará esta clase en
absoluto — pide un `EventPublisher` y se conforma con lo que sea que el
contenedor proporcione.

**Ejecútalo.** La forma más segura de confirmar que todo el grafo se
conecta es el test de integración que arranca el contexto de
aplicación *real* y conduce un depósito a través del bus de comandos.
Desde la raíz del proyecto Lumen:

```bash
uv run --extra dev pytest tests/test_app_context_integration.py -q
```

```text
.                                                                        [100%]
1 passed in 0.19s
```

Ese único punto es el contenedor demostrándose a sí mismo: escaneó los
paquetes, generó el repositorio, resolvió el `EventPublisher` y la
factoría de sesiones, construyó `DepositFundsHandler` con los tres
inyectados y ejecutó de extremo a extremo un ciclo de vida `open →
deposit → withdraw → reload`. Si faltara una dependencia, este test
fallaría en el *arranque* con un `NoSuchBeanError` mucho antes de que
se ejecutara cualquier aserción — que es exactamente el fallo
temprano y ruidoso que el contenedor está diseñado para darte.

::: figure art/figures/02-di.svg | Figura 2.1 — El contenedor inyecta dependencias a partir de las anotaciones de tipo.

!!! spring "Equivalencia con Spring"
    La inyección por constructor en PyFly es funcionalmente idéntica a
    la inyección por constructor con `@Autowired` de Spring. En el
    Spring moderno ni siquiera escribes `@Autowired` — el framework
    infiere la inyección a partir del único constructor, igual que
    PyFly lee las anotaciones de tipo de `__init__`. El modelo mental es
    el mismo: declara lo que necesitas, deja que el contenedor lo
    proporcione.

!!! tip "Consejo"
    Prefiere la inyección por constructor para las dependencias
    obligatorias. Las hace visibles en la firma de la clase, te permite
    escribir tests unitarios de Python puro sin contenedor
    (`handler = DepositFundsHandler(repo=MockRepo(), events=MockBus())`)
    y previene errores accidentales de dependencias faltantes en el
    arranque en lugar de en tiempo de ejecución.

---

## El contenedor y el ApplicationContext

El sistema de DI de PyFly tiene dos capas, y entender la frontera entre
ellas te ahorrará tiempo real de depuración. Una capa gestiona grafos
de objetos; la otra gestiona el ciclo de vida completo de la
aplicación. Confundirlas es una fuente habitual de confusión.

**`Container`** (de `pyfly.container`) es el motor de DI de bajo nivel.
Almacena objetos `Registration`, resuelve tipos por las anotaciones del
constructor, gestiona scopes, aplica la desambiguación con `@primary` y
maneja las búsquedas por nombre basadas en `Qualifier`. No tiene
conciencia del ciclo de vida — es una máquina pura de "dame un `T`".

**`ApplicationContext`** (de `pyfly.context`) es el orquestador de alto
nivel. Envuelve a `Container` y añade toda la secuencia de arranque:
filtrado de perfiles, evaluación de condiciones, procesamiento de
`@configuration`/`@bean`, tejido de `BeanPostProcessor`, hooks de
`@post_construct` / `@pre_destroy`, publicación de eventos y
autoconfiguración. Interactúas con el `ApplicationContext` en el código
de aplicación; el `Container` en crudo es un detalle de
implementación, accesible mediante `ctx.container` como vía de escape.

Piénsalo así: `Container` es la planta de fabricación — sabe cómo
construir cosas. `ApplicationContext` es el jefe de producción —
decide qué se construye, en qué orden y qué pasa cuando la fábrica abre
o cierra.

### Reglas de resolución

Cuando el contenedor necesita resolver un tipo `T`, aplica cuatro
reglas en estricto orden de prioridad:

1. **Registro directo** — si `T` está registrado directamente,
   resuélvelo.
2. **Vínculo de interfaz** — si `T` es un `Protocol` o ABC con
   exactamente una implementación vinculada, resuelve esa
   implementación.
3. **Desambiguación con `@primary`** — si hay múltiples
   implementaciones vinculadas, gana la decorada con `@primary`.
4. **Error** — `NoSuchBeanError` cuando nada coincide;
   `NoUniqueBeanError` cuando existen múltiples candidatos sin
   `@primary`.

El paso 4 es deliberadamente ruidoso. Una dependencia faltante o
ambigua es un error de configuración, y sacarlo a la luz en el arranque
en lugar de enterrarlo en una traza de ejecución es una de las
garantías clave del contenedor.

### @primary

`@primary` resuelve la ambigüedad cuando varios beans satisfacen la
misma interfaz. Ponlo en la implementación que quieres como
predeterminada. Esto surge siempre que tienes un puerto
`@runtime_checkable Protocol` con más de un adaptador registrado — un
patrón habitual para infraestructura intercambiable (almacén de caché,
bus de mensajes, canal de notificación).

Por ejemplo, supón que tu aplicación define un protocolo `CacheStore`
e incluye dos adaptadores — uno en proceso para el desarrollo local y
uno de Redis para producción:

```python
from pyfly.container import repository, primary


@primary
@repository
class InMemoryCacheStore(CacheStore):
    """Default: active in development and tests."""
    ...


@repository
class RedisCacheStore(CacheStore):
    """Production cache — activated by profile or condition."""
    ...
```

Sin `@primary`, resolver `CacheStore` con dos implementaciones
registradas lanza:

```
NoUniqueBeanError: Multiple beans of type 'CacheStore' found
  but none is marked @primary
  Candidates: ['InMemoryCacheStore', 'RedisCacheStore']
```

El mensaje nombra a cada candidato en competencia para que puedas tomar
una decisión deliberada en lugar de adivinar cuál habría elegido el
contenedor. Mover `@primary` de un adaptador al otro es el único cambio
necesario para conmutar el almacén de respaldo de la aplicación — nada
en el código de servicio cambia.

Ten en cuenta que el propio `WalletRepository` de Lumen es una subclase
del `Repository` del framework, así que solo se registra un bean y no
se necesita `@primary`. `@primary` es relevante siempre que construyas a
mano un par puerto/adaptador con múltiples implementaciones de
adaptador.

### @order

El contenedor inicializa los beans singleton de forma anticipada
durante el arranque, pero algunos beans realmente deben estar listos
antes que otros — un filtro de seguridad que debe envolver cada
petición entrante, o un migrador de esquema que debe ejecutarse antes
de tocar cualquier repositorio. `@order` te da control explícito sobre
la secuencia de inicialización.

Los valores más bajos se resuelven primero durante la pasada de
arranque anticipado. Las constantes `HIGHEST_PRECEDENCE` (`-(2**31)`) y
`LOWEST_PRECEDENCE` (`2**31 - 1`) marcan los extremos:

```python
from pyfly.container import order, HIGHEST_PRECEDENCE, service


@order(HIGHEST_PRECEDENCE)
@service
class SecurityInitializer:
    """Must be ready before any other service."""
    ...
```

`@order` afecta a la resolución de singletons durante el arranque, a la
secuencia en la que se ejecutan las instancias de `BeanPostProcessor` y
a la ordenación de los resultados de `get_beans_of_type()`.

### Qualifier — resolución de beans por nombre

La inyección basada en tipos cubre la mayoría de los escenarios. No
obstante, de vez en cuando necesitas realmente una *instancia*
concreta en lugar de cualquier implementación que satisfaga el tipo —
el caso clásico es una clase `@configuration` que produce dos beans del
mismo tipo (digamos, una conexión de base de datos primaria y una de
réplica de lectura) donde un servicio aguas abajo debe recibir una
específica.

Selecciona un bean concreto por nombre con `Annotated[T,
Qualifier("name")]`:

```python
from typing import Annotated
from pyfly.container import Qualifier, service


@service
class ReportService:
    def __init__(
        self,
        db: Annotated[object, Qualifier("analytics_db")],
    ) -> None:
        self.db = db  # receives the bean named "analytics_db"
```

El contenedor llama a `resolve_by_name("analytics_db",
expected_type=T)` y verifica la asignabilidad — un nombre mal escrito
que apunte al tipo equivocado lanza `NoSuchBeanError` con un mensaje
claro en lugar de inyectar silenciosamente el objeto incorrecto.

---

## Factorías de beans: @configuration y @bean

Los decoradores de estereotipo funcionan de maravilla para las clases
que posees, pero no toda dependencia es una clase que controlas. Los
clientes de terceros necesitan argumentos de constructor conocidos solo
en tiempo de ejecución; beans relacionados comparten estado de
configuración; algunas familias de beans se expresan con mayor claridad
como una única factoría. Para todas estas situaciones, PyFly
proporciona el patrón `@configuration` / `@bean` — código de factoría
explícito que aun así participa plenamente en la maquinaria de
resolución y de ciclo de vida del contenedor.

!!! note "Nuevos términos: @configuration y @bean"
    Una clase `@configuration` es un lugar donde poner **métodos
    factoría**. Un método `@bean` es una de esas factorías: el
    contenedor lo *llama* durante el arranque y registra como bean lo
    que sea que devuelva. Recurres a este patrón cuando una dependencia
    no puede simplemente estereotiparse — por ejemplo, un objeto de
    terceros que no posees, o uno que necesita una construcción a
    medida. La **anotación del tipo de retorno** del método es lo que
    el contenedor usa para registrar el resultado, así que es
    obligatoria.

Una clase `@configuration` actúa como una factoría. Sus métodos `@bean`
se llaman durante la secuencia de arranque, y el valor de retorno de
cada método se registra como un bean cuyo tipo proviene de la anotación
de retorno del método. Lee el listado de abajo en dos pasos:

- **Paso 1 — marcar la clase `@configuration`.** Esto le dice al
  contexto que la escanee en busca de métodos `@bean` antes de
  construir cualquier bean de estereotipo.
- **Paso 2 — escribir un método `@bean` con una anotación de retorno.**
  `event_publisher(self) -> EventPublisher` construye un
  `InMemoryEventBus` y — porque la anotación dice `EventPublisher` — lo
  registra *como* un `EventPublisher`. Cualquier cosa que pida un
  `EventPublisher` recibe ahora esta instancia.

::: listing lumen/infra_config.py | Listado 2.4 — Producir un bean EventPublisher mediante @configuration
from pyfly.container import configuration, bean
from pyfly.eda import EventPublisher, InMemoryEventBus


@configuration
class LumenInfraConfig:
    """Wires infrastructure beans that require explicit construction."""

    @bean
    def event_publisher(self) -> EventPublisher:
        """In-memory event bus — replace with Kafka adapter in production."""
        return InMemoryEventBus()
:::

**Cómo funciona.** `@configuration` le dice al contexto que escanee
`LumenInfraConfig` en busca de métodos `@bean` durante el arranque,
antes de construir cualquier bean de estereotipo. La anotación de
retorno `EventPublisher` es la clave: el contexto la lee y registra la
instancia `InMemoryEventBus` producida *como* un `EventPublisher`, no
como un `InMemoryEventBus`. Esa distinción importa — cuando
`DepositFundsHandler` pide después un `EventPublisher`, recibe la
instancia `InMemoryEventBus` sin saber ni importarle el tipo concreto.

Cambiar a un adaptador de Kafka para producción significa reemplazar
`InMemoryEventBus()` por `KafkaEventPublisher(settings.kafka_url)` en
un solo método. El resto del código queda intacto.

!!! note "Nota: cómo obtiene Lumen realmente su EventPublisher"
    El listado de arriba muestra el patrón `@configuration` / `@bean`
    que escribirías para construir a mano un bean. Lumen en sí *no*
    necesita esto para su bus de eventos: establecer `eda.provider:
    memory` en `pyfly.yaml` pide a la autoconfiguración de EDA del
    framework que registre por ti un bean `EventPublisher` (el mismo
    `InMemoryEventBus` que ves en el cableado `events_is
    InMemoryEventBus` al arrancar). Por eso `DepositFundsHandler` puede
    simplemente pedir un `EventPublisher` — la autoconfiguración ya
    suministró uno. Recurre a `@bean` cuando necesites un bean que el
    framework *no* proporciona de fábrica.

Los métodos `@bean` también pueden declarar parámetros; el contenedor
los resuelve automáticamente:

```python
@configuration
class MessagingConfig:

    @bean
    def audited_publisher(self, base: EventPublisher) -> EventPublisher:
        """Wrap the base publisher with audit logging."""
        return AuditingEventPublisher(base)
```

### Parámetros de @bean

| Parámetro | Por defecto | Descripción |
|---|---|---|
| `name` | nombre del método | Nombre del bean para la resolución por nombre. |
| `scope` | `Scope.SINGLETON` | Scope de ciclo de vida del bean producido. |
| `primary` | `False` | Marca este como el candidato primario para su interfaz. |
| `profile` | `""` | Crea el bean solo cuando la expresión de perfil coincide. |

!!! note "Nota"
    La anotación del tipo de retorno en un método `@bean` es
    **obligatoria**. El contexto la lee para saber bajo qué tipo de
    interfaz registrar el bean producido. Omitirla hará que el bean sea
    inalcanzable por tipo.

---

## Scopes

Cada bean tiene un **scope** que controla cuánto vive su instancia.
Acertar con el scope tiene menos que ver con el rendimiento y más con
la corrección: compartir un objeto con estado diseñado para un solo uso
produce condiciones de carrera; recrear un singleton en cada resolución
desperdicia recursos y anula la caché. El enum `Scope` define tres
valores que cubren la inmensa mayoría de las necesidades del mundo
real.

**`Scope.SINGLETON`** (por defecto) — se crea una instancia en la
primera resolución y se reutiliza durante toda la vida de la
aplicación. Los singletons se instancian de forma anticipada durante
`ApplicationContext.start()`, ordenados por `@order`. Casi todos los
beans de aplicación deberían ser singletons.

**`Scope.TRANSIENT`** — se crea una instancia nueva en cada resolución.
Úsalo para objetos con estado, no compartibles:

::: listing lumen/contexts.py | Listado 2.5 — Un bean transitorio para contexto por operación
from pyfly.container import component, Scope


@component(scope=Scope.TRANSIENT)
class TransferContext:
    """Carries state for a single wallet transfer operation."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.rolled_back: bool = False
:::

**Cómo funciona.** `TransferContext` acumula los pasos de una
transferencia de varios saltos para que una saga pueda revertirlos en
orden inverso si algo falla. Compartir una única instancia entre
peticiones concurrentes mezclaría su estado; `Scope.TRANSIENT`
garantiza que cada resolución produzca un `TransferContext` fresco y
vacío. El contenedor sigue gestionando la clase — inyectándola,
filtrándola por perfil, postprocesándola — pero nunca cachea el
resultado.

**`Scope.REQUEST`** — acotado a una sola petición HTTP. Se crea una
instancia nueva cuando llega una petición y se descarta cuando
termina. Úsalo para beans de la capa web que llevan estado específico
de la petición, como el usuario autenticado actual.

```python
from pyfly.container import component, Scope


@component(scope=Scope.REQUEST)
class CurrentUser:
    user_id: str = ""
    roles: list[str] = []
```

Una regla práctica rápida:

- **SINGLETON** — el bean no tiene estado, o su estado es seguro de
  compartir entre todos los llamadores (pools de conexiones, cachés,
  objetos de servicio).
- **TRANSIENT** — el bean acumula estado por operación que no debe
  filtrarse entre operaciones (sagas, builders, portadores de
  contexto).
- **REQUEST** — el bean lleva estado por petición HTTP que debe estar
  aislado entre peticiones concurrentes (usuario autenticado, ID de
  traza acotado a la petición).

---

## Ciclo de vida y condiciones

La construcción y el cableado son solo la mitad de la historia. Los
beans de infraestructura reales necesitan *actuar* después de
construirse — reservar un pool de hilos, precargar una caché,
suscribirse a una cola de mensajes — y necesitan *deshacer* esas
acciones limpiamente al apagarse. PyFly te da dos hooks de ciclo de
vida para esto, además de una familia de decoradores condicionales que
controlan si un bean participa o no en el contenedor en absoluto.

### @post_construct y @pre_destroy

Una vez que el contenedor construye un bean e inyecta todas sus
dependencias, a menudo necesitas una inicialización de una sola vez —
abrir un pool de conexiones, calentar una caché, registrar un oyente.
Marca un método con `@post_construct` y el contexto lo llamará después
de que se complete la construcción. Se admiten tanto métodos síncronos
como `async`:

::: listing lumen/wallet_audit_listener.py | Listado 2.6 — Hooks de ciclo de vida en un bean @service
from pyfly.container import service
from pyfly.context import post_construct, pre_destroy
import logging

logger = logging.getLogger(__name__)


@service
class WalletAuditListenerWithLifecycle:
    def __init__(self) -> None:
        self._entries: list[dict] = []

    @post_construct
    async def on_start(self) -> None:
        logger.info("wallet_audit_listener_ready")

    @pre_destroy
    async def on_stop(self) -> None:
        logger.info("wallet_audit_listener_shutting_down")
:::

**Cómo funciona.** `on_start` se dispara *después* de que el
constructor retorne y todas las dependencias inyectadas estén
establecidas — lo que hace seguro emitir consultas al repositorio,
abrir conexiones o publicar un evento de aplicación. La palabra clave
`async` funciona sin ninguna configuración extra: el contexto llama a
`await on_start()` cuando detecta una corrutina, y recurre a una
llamada directa para los métodos síncronos.

`@pre_destroy` es la contraparte, llamada durante
`ApplicationContext.stop()` antes de descartar el bean. Los beans se
destruyen en orden **inverso** al de inicialización, de modo que un
oyente arrancado después del repositorio se detiene antes que él.

**Ejecútalo.** Añade el bean de arriba a un paquete escaneado, luego
arranca y detén la aplicación para ver cómo se disparan ambos hooks. La
línea de `@post_construct` aparece durante la pasada de arranque;
pulsar `Ctrl-C` dispara la línea de `@pre_destroy`:

```bash
uv run pyfly run --server uvicorn
```

```text
... wallet_audit_listener_ready          # @post_construct, during startup
^C
... shutting_down | app=lumen
... wallet_audit_listener_shutting_down   # @pre_destroy, during shutdown
```

Ver `..._ready` *antes* de `application_started` confirma que el hook
se ejecuta como parte de la pasada de arranque anticipado; ver
`..._shutting_down` después del `Ctrl-C` confirma el desmontaje
simétrico.

::: figure art/figures/02-lifecycle.svg | Figura 2.2 — El ciclo de vida de un bean.

### Beans condicionales

Las condiciones responden a una pregunta poderosa: *¿debería existir
este bean en absoluto, dado el entorno actual?* Son la forma en que la
misma base de código funciona en desarrollo (adaptadores baratos en
memoria), en CI (Testcontainers) y en producción (infraestructura
real) — sin una sola sentencia `if` en tu código de servicio.

Los decoradores condicionales se evalúan con una estrategia de dos
pasadas durante `ApplicationContext.start()`:

**Pasada 1** (antes de procesar el `@configuration` del usuario)
evalúa:
- `@conditional_on_property(key, having_value="...")` — la clave de
  configuración debe existir y, opcionalmente, coincidir con un valor.
- `@conditional_on_class("module.name")` — el módulo Python debe ser
  importable.
- El invocable `condition` en un decorador de estereotipo.

**Pasada 2** (después de procesar el `@configuration` del usuario)
evalúa:
- `@conditional_on_bean(SomeType)` — registra solo si ya existe otro
  bean de ese tipo.
- `@conditional_on_missing_bean(SomeType)` — registra solo si todavía
  no existe ningún bean de ese tipo.

El diseño de dos pasadas es deliberado. Las condiciones de la Pasada 1
dependen de hechos externos — ficheros de configuración y paquetes
instalados — que se conocen antes de construir cualquier bean. Las
condiciones de la Pasada 2 dependen de *qué beans se registraron*,
información disponible solo después de que la Pasada 1 se estabilice.
Procesarlas en orden garantiza que cada condición se evalúe contra una
vista estable y predecible del contenedor.

El patrón más poderoso es **"predeterminado con anulación"** —
proporciona un respaldo que cede automáticamente ante cualquier
implementación proporcionada por el usuario:

::: listing lumen/notifications.py | Listado 2.7 — Predeterminado con anulación usando @conditional_on_missing_bean
from pyfly.container import service
from pyfly.context import conditional_on_missing_bean, conditional_on_property
import logging

logger = logging.getLogger(__name__)


class NotificationPort:
    async def send(self, owner_id: str, message: str) -> None:
        ...


@conditional_on_property("lumen.smtp.host")
@service
class SmtpNotificationAdapter:
    """Real email sender — only active when SMTP is configured."""

    async def send(self, owner_id: str, message: str) -> None:
        logger.info("smtp_send owner=%s", owner_id)


@conditional_on_missing_bean(NotificationPort)
@service
class LoggingNotificationFallback:
    """Log-only fallback — active whenever no real sender is wired."""

    async def send(self, owner_id: str, message: str) -> None:
        logger.info("notification_fallback owner=%s", owner_id)
:::

**Cómo funciona.** Lee las dos clases como una cadena de intención.
`SmtpNotificationAdapter` se activa solo cuando `lumen.smtp.host` está
presente en la configuración, manteniendo los entornos de desarrollo
libres de clientes de correo a medio configurar.
`LoggingNotificationFallback` se activa siempre que no haya registrado
ningún `NotificationPort` real — en la práctica, cualquier entorno
donde SMTP no esté configurado. El respaldo no comprueba *por qué* el
adaptador real está ausente; simplemente llena el hueco.

Por tanto, cualquier manejador que inyecte `NotificationPort` siempre
recibe *algo* — sin `NoSuchBeanError`, sin guarda de `None`. En
desarrollo y en CI obtienes salida de log estructurada; en producción
obtienes correo real. La elección se hace enteramente en la
configuración, sin cambio de código y sin ninguna ramificación en la
lógica de servicio.

!!! tip "Consejo"
    El par `@conditional_on_missing_bean` / `@conditional_on_property`
    es la forma en que funciona toda la autoconfiguración propia de
    PyFly. Cada subsistema (caché, mensajería, cliente HTTP) incluye un
    bean predeterminado que se aparta automáticamente en el momento en
    que registras tu propia implementación.

---

## Lo que construiste {.recap}

Lumen tiene ahora un `WalletEntity` mapeado a la tabla `wallets`, un
`WalletRepository` que hereda del `Repository[WalletEntity, str]` del
framework (lo que aporta una superficie CRUD asíncrona completa, una
consulta derivada y una consulta de specification), y un
`DepositFundsHandler` conectado al repositorio, al publicador de
eventos y a la factoría de sesiones — todo solo mediante anotaciones de
tipo. Viste por qué `@command_handler` y `@query_handler` **deben
apilarse sobre `@service`** — los decoradores CQRS añaden metadatos de
enrutado, pero `@service` es lo que registra el bean. Viste que el
framework autogenera e inyecta la implementación de `Repository` para
que dependas de la propia clase del repositorio por tipo, sin necesidad
de un par puerto/adaptador escrito a mano. También viste cómo
`@primary` resuelve la ambigüedad cuando dos adaptadores compiten por
un puerto construido a mano, cómo `@post_construct` / `@pre_destroy`
delimitan la vida de un bean, y cómo `@conditional_on_missing_bean`
habilita predeterminados que ceden automáticamente ante
implementaciones reales.

El hilo conductor es consistente: tú declaras la intención con
decoradores y anotaciones de tipo; PyFly proporciona las instancias.
Esa separación te permite probar cada clase de forma aislada,
intercambiar adaptadores sin tocar la lógica de negocio y conducir toda
la configuración de la aplicación desde un fichero YAML — todo lo cual
se vuelve esencial a medida que Lumen crece a lo largo del resto del
libro.

---

## Pruébalo tú mismo {.exercises}

1. **Practica `@primary` con un puerto construido a mano.**
   Define un protocolo `CacheStore` con un único método `async def
   get(key)`. Registra dos adaptadores `@repository` — uno en proceso
   y uno "remoto" simulado — ambos heredando `CacheStore`. Arranca la
   aplicación y observa el `NoUniqueBeanError`. Luego añade `@primary`
   al adaptador en proceso y observa cómo el arranque tiene éxito. A
   continuación, prueba a inyectar el simulado por nombre: anota un
   parámetro del constructor como `Annotated[CacheStore,
   Qualifier("remote_cache")]` después de registrarlo con
   `@repository(name="remote_cache")`.

2. **Añade un `@post_construct` que registre metadatos de arranque.**
   Extiende `WalletAuditListener` con un método `async def
   on_ready(self)` decorado con `@post_construct`. Dentro de él,
   registra el nombre de clase de cualquier dependencia inyectada.
   Ejecuta `pyfly run --reload`, arranca el servidor y confirma que la
   línea de log aparece después de los propios mensajes de arranque del
   framework.

3. **Haz un bean condicional a una propiedad.** Añade un
   `WalletAuditService` decorado con
   `@conditional_on_property("lumen.audit.enabled", having_value="true")`.
   Abre `pyfly.yaml` y omite la clave. Verifica que el servicio está
   ausente de la lista de beans al arrancar. Luego añade
   `lumen.audit.enabled: "true"` a `pyfly.yaml` y vuelve a ejecutar —
   confirma que aparece. Así es exactamente como controlas subsistemas
   opcionales sin tocar el código de servicio.
