<span class="eyebrow">Capítulo 5</span>

# Persistencia y el patrón Repositorio {.chtitle}

::: figure art/openers/ch05.svg | &nbsp;

Lumen tiene una API de monederos (wallets) que funciona, pero cada monedero desaparece en el momento en que reinicias el proceso. Ha llegado la hora de hacer que los monederos sean duraderos.

El enfoque ingenuo es esparcir llamadas a `select()` y `session.commit()` de SQLAlchemy por los manejadores de comandos. PyFly ofrece algo mucho mejor: una **capa de repositorios al estilo de Spring Data**. Declaras una interfaz —`class WalletRepository(Repository[WalletEntity, str])`— y el framework *la implementa por ti*. El CRUD asíncrono completo viene gratis. Los métodos de consulta se derivan de sus **nombres**. La paginación, la ordenación, los filtros componibles y las proyecciones de lectura son ciudadanos de primera clase. No hay ningún adaptador escrito a mano ni SQL en el código de la aplicación.

Este capítulo reconstruye la persistencia de Lumen sobre esa capa, exactamente como lo hace el ejemplo en ejecución: la entidad de SQLAlchemy, el repositorio con sus consultas derivadas y por especificación, `Page`/`Pageable`/`Sort`, las proyecciones para las vistas de lectura y la junta transaccional que mantiene íntegro el agregado `Wallet`. Todo lo que aparece aquí se ejecuta contra un fichero SQLite real con cero infraestructura externa: los 41 tests del ejemplo están en verde sobre él. Este capítulo se dirige a PyFly **v26.6.110**.

Construiremos la capa de persistencia pieza a pieza, y en cada hito hay un recuadro **Ejecútalo** con el comando exacto que debes escribir y la salida que deberías ver. Si estás siguiendo el ejemplo Lumen, trabaja desde la raíz del proyecto (`samples/lumen`), donde viven `pyfly.yaml` y `pyproject.toml`; todos los comandos de abajo dan por hecho ese directorio.

!!! note "Ejecútalo: ve el problema primero"
    Antes de añadir la persistencia, vale la pena sentir el hueco que cierra. Abre un monedero, luego detén y reinicia el proceso: con el almacén en memoria de la Parte I el monedero ha desaparecido. El resto de este capítulo hace que sobreviva a ese reinicio.

    ```bash
    # Terminal 1 — start the app
    uv run pyfly run --server uvicorn
    # ... startup banner, then: Uvicorn running on http://0.0.0.0:8080

    # Terminal 2 — open a wallet, then read it back
    curl -s -X POST localhost:8080/api/v1/wallets \
      -H 'content-type: application/json' \
      -d '{"owner_id": "alice", "currency": "EUR"}'
    ```

    Recuperas el nuevo id, y luego la lectura del saldo confirma que existe:

    ```
    {"wallet_id": "wlt-7f3c..."}
    ```

    Ahora pulsa `Ctrl+C` en la Terminal 1, arranca la aplicación de nuevo y pide el saldo de ese monedero. Antes de este capítulo, la fila nunca se escribió en disco, así que ha desaparecido:

    ```
    {"detail": "Wallet 'wlt-7f3c...' not found", "code": "WALLET_NOT_FOUND"}
    ```

    Al final de este capítulo la misma secuencia devuelve el saldo después de un reinicio.

---

## El repositorio, en una frase

::: figure art/figures/05-repository.svg | Figura 5.1 — Tu código depende del repositorio; el framework suministra la implementación de SQLAlchemy que hay detrás.

Un repositorio de PyFly es una clase que hereda del genérico `Repository[Entity, ID]` y va marcada con el estereotipo `@repository`. Esa es toda la declaración. A partir de los dos parámetros de tipo el framework aprende el **tipo de entidad** y el **tipo de la clave primaria**, y desde ahí proporciona una superficie completa de acceso a datos asíncrono —`save`, `find_by_id`, `find_all`, `delete`/`delete_by_id`, `count`, `exists_by_id`, además de paginación y consultas por especificación— con la `AsyncSession` de la base de datos inyectada por ti.

Este es el patrón Repositorio tal como lo popularizó Spring Data, trasladado a un Python asíncrono idiomático. Tú escribes *qué* quieres (el método) y el framework escribe *cómo* (el SQL).

!!! spring "Equivalencia con Spring"
    `Repository[T, ID]` es el `JpaRepository<T, ID>` de PyFly. Heredar de él para obtener el CRUD, derivar consultas a partir de los nombres de método, `Pageable`/`Page`, `Specification` y las proyecciones por interfaz están todos trasladados casi nombre por nombre desde Spring Data JPA. Si has escrito un `interface OrderRepository extends JpaRepository<Order, UUID>` de Spring, ya conoces la forma de este capítulo.

---

## La entidad: una fila por monedero

Antes de que un repositorio pueda almacenar algo, necesita una **entidad**: la forma en disco de un monedero, una fila plana por agregado. (Una *entidad*, en esta capa, es simplemente una clase de Python que se mapea a una tabla de la base de datos; cada instancia es una fila.) Las entidades de PyFly son modelos ordinarios de SQLAlchemy 2.0 construidos sobre una base declarativa que el framework exporta.

La construiremos campo a campo. Crea el fichero `src/lumen/models/entities/v1/wallet_orm.py` y añade las piezas por orden.

**Paso 1 — importa la base declarativa del framework.** Cada entidad hereda de una *base declarativa*: una clase de SQLAlchemy que registra cada tabla que defines para que el framework pueda crearlas todas al arrancar. PyFly exporta una como `Base`:

```python
from pyfly.data.relational.sqlalchemy import Base
```

**Paso 2 — nombra la tabla y declara las columnas.** Hereda de `Base`, fija `__tablename__` y escribe un atributo tipado por columna. La entidad completa es corta:

::: listing lumen/models/entities/v1/wallet_orm.py | Listado 5.1 — WalletEntity: la fila de persistencia de SQLAlchemy
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's string id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC)
    )
:::

La sintaxis `Mapped[T]` / `mapped_column(...)` es el estilo de SQLAlchemy 2.0: cada anotación de tipo dirige tanto el tipo del atributo de Python como el DDL de columna generado (la sentencia `CREATE TABLE`), de modo que cada columna tiene una única fuente de verdad. Como `WalletEntity` hereda de `Base`, importar este módulo registra la tabla `wallets` en `Base.metadata` —el registro de todas las tablas que conoce la base—, y el ciclo de vida del motor del framework la crea entonces al arrancar.

!!! note "Qué acaba de pasar"
    Escribiste una clase, sin SQL. Los cinco atributos tipados se convirtieron en cinco columnas; `primary_key=True` marcó `id` como la clave; `index=True` en `owner_id` acelerará la consulta de "monederos propiedad de X" que construyes más adelante; `nullable=False` y `default=...` fijan las restricciones. Importar este módulo basta para que el framework sepa que la tabla existe: nunca llamas a `CREATE TABLE` tú mismo.

Hay dos decisiones de diseño que conviene destacar.

**Base, no BaseEntity.** PyFly trae dos bases declarativas. `BaseEntity` te da una clave primaria sustituta de tipo **UUID** más cuatro columnas de auditoría (`created_at`, `updated_at`, `created_by`, `updated_by`) rellenadas automáticamente: el valor por defecto adecuado para la mayoría de las tablas. Lumen usa deliberadamente la `Base` simple en su lugar, porque el agregado `Wallet` ya es dueño de su identidad: un id de cadena con la forma `wlt-…`. Heredar de `Base` deja que la fila conserve esa clave primaria de **cadena**, de modo que la fila y el agregado comparten una identidad en lugar de que la fila invente una segunda, sustituta.

**Unidades menores en enteros.** Los importes viven en `balance_minor` como céntimos enteros, nunca como un número en coma flotante. Las columnas de coma flotante acumulan error de redondeo a lo largo de millones de transacciones; la aritmética entera se mantiene exacta. Un saldo de `2500` significa 25,00 €: el decimal en unidad mayor se calcula solo en los bordes, para mostrarlo.

!!! tip "Recurre a BaseEntity por defecto"
    A menos que tu agregado sea dueño de una clave natural como lo es `Wallet`, prefiere `class Order(BaseEntity)`. Obtienes una PK de tipo UUID y columnas de auditoría gratis, y el `AuditingEntityListener` rellena `created_by`/`updated_by` a partir del contexto de seguridad en cada inserción y actualización. Lumen es la excepción, no la regla.

---

## El repositorio: CRUD gratis

Ahora, la pieza central. El `WalletRepository` de Lumen hereda de `Repository[WalletEntity, str]` —tipo de entidad `WalletEntity`, tipo de clave primaria `str`— y se registra con `@repository`. (Un *estereotipo* como `@repository` es un decorador de clase que le dice al contenedor del framework: "gestióname una instancia de esto". Esa instancia gestionada es un **bean**: un objeto que el contenedor crea una vez y entrega a cualquiera que lo pida. La *inyección de dependencias*, o *DI*, es que el contenedor haga esa entrega por ti, de modo que un manejador nunca construye su propio repositorio.) Esa única declaración basta para que el framework suministre toda la superficie CRUD —siendo *CRUD* las cuatro operaciones básicas de tabla: Crear, Leer, Actualizar, Borrar (Create, Read, Update, Delete).

Crea `src/lumen/models/repositories/wallet_repository.py` en dos pasos.

**Paso 1 — importa lo que la declaración necesita.** Tres imports: tu entidad, el estereotipo `@repository` y la base genérica `Repository`.

```python
from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Repository
```

**Paso 2 — hereda de la base genérica y márcala con `@repository`.** Los dos parámetros de tipo llevan todo el cableado:

::: listing lumen/models/repositories/wallet_repository.py | Listado 5.2 — WalletRepository: heredar del repositorio del framework
from __future__ import annotations

from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data import Page, Pageable
from pyfly.data.relational.sqlalchemy import Repository, Specification


@repository
class WalletRepository(Repository[WalletEntity, str]):
    """CRUD + derived + specification queries for WalletEntity.

    The @repository stereotype registers this as a DI bean. The
    framework reads the entity/PK types from the
    Repository[WalletEntity, str] base and injects the shared
    AsyncSession.
    """

    # (query methods follow — see the next sections)
:::

No hay `__init__`, ni SQL, ni clase adaptadora. Con solo esa declaración, cualquier manejador que inyecte un `WalletRepository` ya puede llamar a:

| Método                          | Devuelve            | Qué hace                                       |
|---------------------------------|---------------------|------------------------------------------------|
| `save(entity)`                  | `T`                 | Inserta o actualiza; **vuelca** (flush) + refresca |
| `find_by_id(id)`                | `T \| None`         | Carga por clave primaria                       |
| `find_all(**filters)`           | `list[T]`           | Todas las filas, filtros de igualdad opcionales |
| `find_all(sort)`                | `list[T]`           | Todas las filas en un orden `Sort` dado        |
| `find_all(pageable)`            | `Page[T]`           | Consulta paginada + ordenada (ver abajo)       |
| `stream_all(sort)`              | `AsyncIterator[T]`  | Recorre en flujo cada fila (el análogo de `Flux<T>`) |
| `delete(entity)`                | `None`              | Borra una entidad dada                         |
| `delete_by_id(id)`              | `None`              | Borra por clave primaria (sin efecto si no existe) |
| `delete_all(entities=None)`     | `None`              | Borra las entidades dadas (o todas las filas)  |
| `delete_all_by_id(ids)`         | `None`              | Borra muchas filas por clave primaria          |
| `count()`                       | `int`               | Cuenta todas las filas de la tabla             |
| `exists_by_id(id)`              | `bool`              | Si existe una fila con este id                 |
| `save_all(entities)`            | `list[T]`           | Inserción/actualización en bloque              |
| `find_all_by_id(ids)`           | `list[T]`           | Carga muchas filas por clave primaria          |
| `find_all_by_spec(spec)`        | `list[T]`           | Filas que satisfacen una `Specification`       |
| `find_all_by_spec_paged(...)`   | `Page[T]`           | Consulta `Specification` paginada + ordenada   |

Eso es más que suficiente para la mayoría de las entidades. Lumen añade tres métodos propios por encima —una consulta derivada, una consulta por especificación y un upsert— que las siguientes secciones van construyendo.

!!! spring "Equivalencia con Spring"
    Esta superficie heredada es exactamente la jerarquía de repositorios de Spring Data, trasladada nombre por nombre y estable a partir de PyFly **v26.6.110**: `CrudRepository` → `ReactiveSortingRepository` → `PagingAndSortingRepository`. `save`/`save_all`, `find_by_id`, `find_all`, `exists_by_id`, `count` y la familia `delete*` se corresponden con sus equivalentes de Spring; `find_all(pageable) -> Page[T]` es `findAll(Pageable)`, y `find_all_by_spec*` es el `JpaSpecificationExecutor`. Si conoces `JpaRepository<T, ID>`, ya conoces esta tabla.

### Cómo conoce los tipos el framework

Cuando escribes `Repository[WalletEntity, str]`, el hook `__init_subclass__` de la clase base inspecciona `__orig_bases__` en el momento de la definición de la clase y extrae el tipo de entidad (`WalletEntity`) y el tipo del id (`str`) de los parámetros genéricos. (`__init_subclass__` es un hook de Python que se ejecuta una vez, de forma automática, cuando se *define* una subclase, así que esto ocurre en tiempo de importación, antes de crear ningún objeto.) La `AsyncSession` —el manejador de conexión-y-transacción de la base de datos por el que pasa cada consulta— se suministra entonces como dependencia inyectada por la autoconfiguración relacional. No se pasa nada manualmente: los parámetros de tipo *son* el cableado.

!!! note "Ejecútalo: confirma que el repositorio se cablea"
    La prueba más rápida de que la entidad y el repositorio están sanos es la batería de tests, que ejercita el repositorio contra un fichero SQLite real sin servidor. Desde la raíz del proyecto Lumen:

    ```bash
    uv run --extra dev pytest tests/test_sql_wallet_repository.py -q
    ```

    Deberías ver pasar todos los tests del repositorio:

    ```
    ......                                                            [100%]
    6 passed in 0.30s
    ```

    Estos tests construyen el repositorio directamente y ejercitan `upsert`, `find_by_id`, `count`, la consulta derivada y la ruta de especificación: los mismos métodos que construye este capítulo. Si están en verde, las columnas de tu entidad y la declaración `Repository[WalletEntity, str]` son correctas.

---

## Consultas derivadas: el nombre del método es la consulta

El CRUD cubre las búsquedas por clave primaria. Las aplicaciones reales también necesitan consultar por otras columnas: "todos los monederos propiedad de este cliente". En la mayoría de los frameworks escribirías el SQL a mano. En PyFly declaras un **esbozo** (stub) —un método sin cuerpo, solo `...`— y dejas que el framework compile la consulta *a partir del nombre del método*.

**Paso 1 — declara el esbozo.** Añade un método a `WalletRepository`. El *nombre* describe la consulta; el cuerpo es literalmente `...`:

::: listing lumen/models/repositories/wallet_repository.py | Listado 5.3 — Una consulta derivada: declarada como esbozo, compilada a partir de su nombre
@repository
class WalletRepository(Repository[WalletEntity, str]):

    # derived query: compiled from the method name by the post-processor
    async def find_by_owner_id(
        self, owner_id: str
    ) -> list[WalletEntity]:
        """All wallets owned by *owner_id* (derived query stub)."""
        ...
:::

**Paso 2 — deja que el framework rellene el cuerpo.** No escribes más código. Al arrancar, un `BeanPostProcessor` —el `RepositoryBeanPostProcessor`— hace el trabajo. (Un *BeanPostProcessor* es un hook que el contenedor ejecuta sobre cada bean justo después de crearlo; este se especializa en repositorios.) Examina el repositorio, detecta que `find_by_owner_id` es un esbozo, analiza el **nombre** convirtiéndolo en una consulta parseada y reemplaza el esbozo por una implementación real que ejecuta `SELECT … FROM wallets WHERE owner_id = :owner_id`. Llamar a `await repo.find_by_owner_id("alice")` ahora devuelve exactamente las filas de ese propietario.

!!! note "Qué acaba de pasar"
    Declaraste un método y obtuviste una consulta funcional: el framework leyó la *intención* del nombre `find_by_owner_id` y escribió el SQL por ti. La nomenclatura no es magia; sigue una gramática, que se cubre a continuación. La idea clave: en esta capa describes *qué* quieres mediante cómo nombras el método, y el post-procesador suministra el *cómo*.

La gramática es la convención de Spring Data. Un nombre de método es un **prefijo** seguido de un **sujeto** construido a partir de nombres de campo, operadores, conectores y una cláusula de ordenación opcional:

| Parte       | Tokens                                                                   |
|-------------|--------------------------------------------------------------------------|
| Prefijo     | `find_by` · `count_by` · `exists_by` · `delete_by`                       |
| Conectores  | `_and_` · `_or_`                                                          |
| Operadores  | `_greater_than` · `_less_than` · `_between` · `_in` · `_like` · `_containing` · `_is_null` · `_is_not_null` |
| Ordenación  | `_order_by_<field>_<asc\|desc>`                                           |

Cada cláusula consume el número correspondiente de argumentos del método (la igualdad y las comparaciones toman uno; `_between` toma dos; `_is_null` / `_is_not_null` no toman ninguno). Algunos ejemplos sobre un hipotético repositorio de pedidos:

```python
@repository
class OrderRepository(Repository[Order, UUID]):
    async def find_by_status(self, status: str) -> list[Order]: ...

    async def find_by_customer_id_and_status(
        self, customer_id: str, status: str
    ) -> list[Order]: ...

    async def find_by_total_greater_than(
        self, min_total: float
    ) -> list[Order]: ...

    async def find_by_total_between(
        self, low: float, high: float
    ) -> list[Order]: ...

    async def count_by_status(self, status: str) -> int: ...

    async def exists_by_customer_id(self, customer_id: str) -> bool: ...

    async def find_by_status_order_by_created_at_desc(
        self, status: str
    ) -> list[Order]: ...
```

El prefijo decide la *forma* del resultado: `find_by` devuelve una lista, `count_by` devuelve un `int`, `exists_by` devuelve un `bool` y `delete_by` emite un `DELETE` y devuelve el número de filas eliminadas. Nunca escribes el SQL; nombras el método y anotas el tipo de retorno.

!!! tip "Cuando un nombre se vuelva absurdo, usa @query"
    Los nombres derivados son perfectos hasta dos o tres predicados. Pasado eso, se vuelven ilegibles. Para cualquier cosa más compleja, coloca un decorador `@query("SELECT w FROM WalletEntity w WHERE …")` (al estilo JPQL, o `native=True` para SQL en bruto) sobre el esbozo y escribe la consulta de forma explícita. El mismo patrón de esbozo más decorador; solo que tú suministras el texto de la consulta en lugar de codificarlo en el nombre.

---

## Paginación: Page, Pageable y Sort

Un endpoint de listado nunca debería devolver *todos* los monederos. La *paginación* es la solución estándar: devolver una **página** de filas de tamaño fijo a la vez, más los metadatos suficientes para que el cliente pueda pedir la siguiente. Los tipos de paginación de PyFly —`Pageable` (qué página, qué tamaño, qué orden), `Sort` (la ordenación) y `Page[T]` (el fragmento más los metadatos)— se heredan directamente de la superficie CRUD a través de `find_all(pageable)`.

Hay tres piezas pequeñas que ensamblar: el manejador que llama a `find_all(pageable)`, el `Page[T]` que devuelve y el controlador que construye el `Pageable` a partir de la petición. Las veremos en ese orden.

El manejador de consulta `ListWallets` de Lumen es toda la historia en tres líneas:

::: listing lumen/core/services/wallets/list_wallets_handler.py | Listado 5.4 — Paginando con find_all(pageable), y luego mapeando la página
@query_handler
@service
class ListWalletsHandler(
    QueryHandler[ListWallets, Page[WalletDto]]
):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(  # type: ignore[override]
        self, query: ListWallets
    ) -> Page[WalletDto]:
        page = await self._repository.find_all(
            query.pageable
        )
        return page.map(entity_to_dto)
:::

`find_all(pageable)` hace tres cosas en una sola llamada: cuenta el número total de filas coincidentes, aplica la ordenación del `Pageable` y corta el resultado con `LIMIT`/`OFFSET`. Devuelve un `Page[WalletEntity]`. El manejador llama entonces a `page.map(entity_to_dto)` para convertir cada fila en un `WalletDto` **sin perder los metadatos de paginación**: `.map` traslada `total`, `page`, `size` y el resto a la nueva página.

Un `Page[T]` expone todo lo que un cliente necesita para renderizar un paginador:

| Miembro         | Significado                              |
|-----------------|------------------------------------------|
| `items`         | Las filas de esta página (`list[T]`)     |
| `total`         | Total de filas coincidentes en todas las páginas |
| `page`          | Número de página actual (base 1)         |
| `size`          | Máximo de elementos por página           |
| `total_pages`   | `ceil(total / size)`                     |
| `has_next`      | Si existe una página siguiente           |
| `has_previous`  | Si existe una página anterior            |
| `map(fn)`       | Transforma los elementos, conservando los metadatos |

El propio `Pageable` se construye en el borde: el controlador convierte los parámetros de consulta `?page=&size=` en un `Pageable` con un `Sort` compartido de más reciente primero:

::: listing lumen/web/controllers/wallet_controller.py | Listado 5.5 — Construyendo un Pageable a partir de los parámetros de consulta (controlador)
#: Newest-first ordering shared by the list endpoints.
_NEWEST_FIRST = Sort.by("created_at").descending()


@get_mapping("")
async def list_wallets(
    self, page: QueryParam[int] = 1, size: QueryParam[int] = 20
) -> PageDto[WalletDto]:
    """A page of wallets, newest first."""
    result = await self._queries.query(
        ListWallets(pageable=Pageable.of(page, size, _NEWEST_FIRST))
    )
    return PageDto.from_page(result)
:::

`Sort.by("created_at").descending()` nombra la columna y la dirección; `Pageable.of(page, size, sort)` lo empaqueta con las coordenadas de la página. El manejador devuelve un `Page` del framework, y el controlador lo pliega en un `PageDto` serializable —un reflejo Pydantic plano de la página— de modo que `GET /api/v1/wallets?page=1&size=20` devuelve un JSON como `{"items": [...], "total": 42, "page": 1, "total_pages": 3, "has_next": true, ...}`.

!!! note "Ejecútalo: pasa por las páginas de los monederos"
    Con la capa relacional activada (la sección "Activarlo" de más abajo la enciende; el ejemplo Lumen ya la trae habilitada), abre un par de monederos y luego pide la primera página. Desde una aplicación en ejecución:

    ```bash
    curl -s 'localhost:8080/api/v1/wallets?page=1&size=20'
    ```

    La respuesta lleva las filas *y* los metadatos del paginador: fíjate en `total`, `page`, `total_pages` y `has_next` junto a `items`:

    ```json
    {
      "items": [
        {"id": "wlt-...", "owner_id": "alice", "currency": "EUR",
         "balance_minor": 0, "balance": 0.0, "created_at": "..."}
      ],
      "total": 1, "page": 1, "size": 20, "total_pages": 1,
      "has_next": false, "has_previous": false
    }
    ```

    Esos son exactamente los miembros de `Page[T]` de la tabla de arriba, serializados por `PageDto`. El cliente renderiza un paginador directamente a partir de esta forma, sin necesidad de una consulta de conteo adicional.

---

## Especificaciones: filtros componibles y reutilizables

Las consultas derivadas responden a preguntas fijas. A veces quieres un **predicado reutilizable** —una condición `WHERE` que puedes nombrar una vez y reutilizar— que compones en el lugar de la llamada: "monederos con al menos este saldo", combinado libremente con otras condiciones. Eso es lo que es una `Specification`: un objeto pequeño que envuelve un fragmento `WHERE`, componible con `&` (AND), `|` (OR) y `~` (NOT).

La construimos en dos pasos: una factoría que *devuelve* una `Specification`, y luego un método de repositorio que la *ejecuta*.

**Paso 1 — escribe una factoría que devuelva una `Specification`.** Toma el parámetro (el saldo mínimo) y devuelve un objeto predicado. **Paso 2 — añade un método de repositorio que lo ejecute** a través del `find_all_by_spec_paged` heredado. Lumen hace ambas cosas en un solo fichero:

::: listing lumen/models/repositories/wallet_repository.py | Listado 5.6 — Una factoría de Specification y un método que la ejecuta paginada
def balance_at_least(min_minor: int) -> Specification[WalletEntity]:
    """Wallets whose balance is at least *min_minor*.

    Returned as a Specification, so it composes via & / | / ~ and
    runs through find_all_by_spec / find_all_by_spec_paged.
    """
    return Specification(
        lambda root, q: q.where(root.balance_minor >= min_minor)
    )


@repository
class WalletRepository(Repository[WalletEntity, str]):

    async def find_rich(
        self, min_minor: int, pageable: Pageable
    ) -> Page[WalletEntity]:
        """A page of wallets with balance >= min_minor."""
        return await self.find_all_by_spec_paged(
            balance_at_least(min_minor), pageable
        )
:::

Una `Specification` envuelve un invocable `(root, q) -> q`: dada la clase de entidad (`root`) y un `Select` de SQLAlchemy, devuelve la sentencia con un predicado añadido. `balance_at_least(1000)` produce el predicado `balance_minor >= 1000`. Como las especificaciones se componen con operadores de Python, puedes construir filtros arbitrariamente complejos a partir de piezas pequeñas:

```python
rich = balance_at_least(1000)
in_eur = Specification(
    lambda root, q: q.where(root.currency == "EUR")
)
rich_eur = rich & in_eur          # AND
rich_or_eur = rich | in_eur       # OR
not_rich = ~rich                  # NOT
```

Una especificación se ejecuta de dos maneras. `find_all_by_spec(spec)` devuelve como lista todas las filas coincidentes; `find_all_by_spec_paged(spec, pageable)` aplica el predicado, cuenta las coincidencias, ordena y corta, devolviendo un `Page[T]`. `find_rich` usa la forma paginada, así que el endpoint de monederos ricos está él mismo paginado. El manejador refleja exactamente el manejador de listado, mapeando las filas a DTOs:

::: listing lumen/core/services/wallets/list_rich_wallets_handler.py | Listado 5.7 — El manejador de monederos ricos ejecuta la ruta de Specification
@query_handler
@service
class ListRichWalletsHandler(
    QueryHandler[ListRichWallets, Page[WalletDto]]
):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(  # type: ignore[override]
        self, query: ListRichWallets
    ) -> Page[WalletDto]:
        page = await self._repository.find_rich(
            query.min_minor, query.pageable
        )
        return page.map(entity_to_dto)
:::

`GET /api/v1/wallets/rich?min_minor=1000&page=1&size=20` devuelve ahora una página de monederos con 10,00 € o más, de más reciente primero.

!!! note "Ejecútalo: filtra a los monederos ricos"
    Abre un monedero e ingresa 25,00 € en él (2500 unidades menores), abre otro y déjalo vacío, y luego pide los monederos con al menos 10,00 €:

    ```bash
    curl -s 'localhost:8080/api/v1/wallets/rich?min_minor=1000&page=1&size=20'
    ```

    Solo vuelve el monedero con fondos, y `total` cuenta únicamente las coincidencias: el monedero vacío queda filtrado por la `Specification`:

    ```json
    {"items": [{"id": "wlt-...", "balance_minor": 2500, "balance": 25.0, ...}],
     "total": 1, "page": 1, "total_pages": 1, "has_next": false}
    ```

    El mismo predicado (`balance_at_least`) que se ejecuta aquí puede combinarse con otros usando `&`, `|` y `~`: esa es la recompensa de escribir el filtro como una `Specification` en lugar de como una consulta puntual.

!!! note "Filtros sin lambdas"
    Para el caso común —igualdad y un puñado de comparaciones— ni siquiera necesitas escribir una lambda. `FilterOperator.gte("balance_minor", 1000) & FilterOperator.eq("currency", "EUR")` produce la misma `Specification` componible a partir de métodos factoría estáticos, y `FilterUtils.by(currency="EUR")` construye una a partir de argumentos por palabra clave (consulta por ejemplo, Query-by-Example). Lumen usa aquí una lambda explícita porque la intención se lee con claridad; ambos estilos producen una `Specification` que puedes pasar a los mismos métodos del repositorio.

---

## Proyecciones: lee solo las columnas que necesitas

El endpoint de saldo no necesita la fila entera, solo el id, la moneda y un saldo calculado. PyFly admite **proyecciones por interfaz**, la idea de Spring Data de declarar el subconjunto de campos que quiere una vista de lectura y dejar que el framework copie exactamente esos. (Una *proyección* es una vista de solo lectura sobre un subconjunto de las columnas de una entidad: nombras los pocos campos que te importan, y el framework copia únicamente esos, dejando sin leer el resto de la fila.)

Construir una requiere tres piezas: la clase de proyección, el mapeador que sabe cómo rellenarla y el manejador que la usa. Las veremos por orden.

**Paso 1 — declara la proyección.** Una proyección es una clase marcada con `@projection`. En Lumen es una dataclass concreta:

::: listing lumen/interfaces/dtos/v1/balance_dto.py | Listado 5.8 — BalanceView: una @projection solo de los campos del saldo
from dataclasses import dataclass

from pyfly.data import projection


@projection
@dataclass
class BalanceView:
    """Projection: just the fields the balance view needs.

    id, currency and balance_minor are copied straight from the
    WalletEntity; balance is a computed major-unit decimal supplied
    by a registered transform on the mapper.
    """

    id: str
    currency: str
    balance_minor: int
    balance: float
:::

**Paso 2 — registra la proyección en un `Mapper`.** Un `Mapper` es el ayudante del framework que copia los campos de la entidad en una proyección. Lee esos cuatro campos de un `WalletEntity` y construye la vista. Tres (`id`, `currency`, `balance_minor`) se copian tal cual; el cuarto (`balance`, el decimal en unidad mayor) lo suministra un *transform*: una pequeña función registrada contra un nombre de campo que calcula un valor que la entidad no almacena directamente:

::: listing lumen/core/mappers/wallet_mapper.py | Listado 5.9 — Registrando y ejecutando la proyección mediante Mapper
from pyfly.data import Mapper

_mapper = Mapper()
_mapper.register_projection(
    WalletEntity,
    BalanceView,
    transforms={"balance": lambda e: round(e.balance_minor / 100, 2)},
)


def entity_to_balance_dto(entity: WalletEntity) -> BalanceDto:
    """Project a row onto the balance DTO via the projection."""
    view = _mapper.project(entity, BalanceView)
    return BalanceDto(
        id=view.id,
        currency=Currency(view.currency),
        balance_minor=view.balance_minor,
        balance=view.balance,
    )
:::

**Paso 3 — usa la proyección desde un manejador de lectura.** `Mapper.project(entity, BalanceView)` lee solo los campos declarados, aplica el transform de `balance` y devuelve un `BalanceView`. El manejador de consulta carga entonces la fila por id y la proyecta:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listado 5.10 — El manejador de lectura del saldo: busca por id y luego proyecta
@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(  # type: ignore[override]
        self, query: GetBalance
    ) -> BalanceDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return (
            entity_to_balance_dto(entity)
            if entity is not None
            else None
        )
:::

!!! note "Ejecútalo: lee solo el saldo"
    El endpoint de saldo devuelve únicamente los cuatro campos proyectados, no la fila entera. Contra una aplicación en ejecución con un monedero con fondos:

    ```bash
    curl -s localhost:8080/api/v1/wallets/wlt-.../balance
    ```

    ```json
    {"id": "wlt-...", "currency": "EUR", "balance_minor": 2500, "balance": 25.0}
    ```

    Sin `owner_id`, sin `created_at`: la proyección declaró cuatro campos, así que se leen y se devuelven cuatro campos. `balance` (el `25.0` en unidad mayor) es el transform calculado; el resto se copia directamente de la fila.

!!! warning "Una proyección debe poder instanciarse"
    Spring permite que una proyección sea una interfaz pelada y devuelve un proxy en tiempo de ejecución. Python no tiene tal proxy, así que una proyección de PyFly debe ser un tipo **concreto** que el mapeador pueda construir: aquí, una `@dataclass`. Marcar un *Protocol* como `@projection` no funcionará: un Protocol no puede instanciarse, y `Mapper.project` no tiene nada que construir. Usa una dataclass (o cualquier clase plana con campos coincidentes) y estarás a salvo.

---

## Transacciones y la junta del agregado

La superficie del repositorio es limpia, pero dos sutilezas honestas deciden si tus escrituras realmente sobreviven. Ambas provienen de cómo gestiona el framework la sesión, y Lumen maneja ambas de forma deliberada.

### save() vuelca (flush); no confirma (commit)

Esto es lo más importante que hay que entender sobre la capa de datos. Hay dos verbos de base de datos fáciles de confundir. **Volcar** (flush) es enviar el SQL pendiente (el `INSERT`/`UPDATE`) a la base de datos para que sea visible a las lecturas posteriores de *esta* conexión, pero todavía dentro de una transacción abierta que puede deshacerse. **Confirmar** (commit) es hacer esos cambios permanentes y visibles para todos. Un flush sin commit se revierte cuando se cierra la sesión.

El framework usa **una sola `AsyncSession` compartida**, y `Repository.save()` llama a `session.add()` seguido de `session.flush()` y `session.refresh()`: **vuelca**, haciendo la escritura visible *dentro* de la sesión actual, pero nunca **confirma**. Si nada confirma, la escritura se revierte cuando se cierra la sesión y el monedero no sobrevive a un reinicio. (Este es exactamente el problema del monedero que desaparece del recuadro **Ejecútalo** de la introducción.)

El commit ocurre en el **límite de la unidad de trabajo**. (Una *unidad de trabajo* es un lote de cambios de todo o nada: o bien cada escritura que contiene se confirma junta, o bien —si algo falla— ninguna lo hace.) Declaras ese límite con `@transactional()`. Un manejador que escribe decora su `do_handle` con `@transactional()`, inyecta el `async_sessionmaker` —la factoría que entrega sesiones— como `self._session_factory`, y el decorador abre una unidad de trabajo, intercambia esa sesión transaccional en el repositorio durante la llamada, **confirma si tiene éxito** y revierte si falla:

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listado 5.11 — Un manejador de escritura: @transactional() confirma la unidad de trabajo
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
        # @transactional resolves the unit-of-work session from here.
        self._session_factory = session_factory

    @transactional()
    async def do_handle(  # type: ignore[override]
        self, command: OpenWallet
    ) -> str:
        wallet_id = f"wlt-{uuid4()}"
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(
            self._events, wallet.clear_events()
        )
        return wallet_id
:::

`@transactional()` (importado de `pyfly.data.relational.sqlalchemy`) resuelve el `async_sessionmaker` desde `self._session_factory`, ejecuta el cuerpo dentro de un bloque `session.begin()` y confirma al final. Quita el decorador y el `upsert` solo volcaría: el monedero nunca llegaría al disco. Los manejadores de lectura anteriores de este capítulo no necesitan `@transactional`: una lectura no hace cambios que confirmar.

!!! note "Qué acaba de pasar"
    La regla en una línea: **las lecturas no necesitan nada; las escrituras necesitan `@transactional()`.** `save`/`upsert` solo *vuelcan*, así que un manejador de escritura debe ejecutarse dentro de una unidad de trabajo que confirme. El decorador hace tres cosas por ti: abre la transacción, entrega al repositorio la sesión correcta para la llamada y confirma (o revierte ante una excepción). Por eso el monedero recién abierto sobrevivió una vez activada la persistencia, y por eso quitar el decorador lo perdería silenciosamente.

### upsert, no save, para un agregado que es dueño de su id

Fíjate en que el manejador llama a `self._repository.upsert(...)`, no a `save(...)`. (*Upsert* es el verbo combinado para "insertar si es nuevo, actualizar si ya está presente": una sola llamada para ambos casos.) Esa es la segunda sutileza. El `save()` del framework emite `session.add()`, que SQLAlchemy trata como un **INSERT pendiente**. Pero el agregado `Wallet` genera su *propia* clave primaria por adelantado (`wlt-…`), así que para cuando un ingreso o una retirada persisten un monedero ya cargado, ya existe una fila con ese id, y un segundo `INSERT` sobre la misma clave primaria lanza `IntegrityError`.

El arreglo es `session.merge`, que inserta cuando el id es nuevo y actualiza cuando ya existe. Lumen lo envuelve en un método de conveniencia `upsert`:

::: listing lumen/models/repositories/wallet_repository.py | Listado 5.12 — upsert: una sola llamada tanto para INSERT como para UPDATE
@repository
class WalletRepository(Repository[WalletEntity, str]):

    async def upsert(self, entity: WalletEntity) -> WalletEntity:
        """Insert *entity* or update the existing row with the same id.

        Uses session.merge so a freshly-mapped entity carrying the
        aggregate's id persists whether or not a row already exists —
        the aggregate owns its primary key, so identity is never
        ambiguous. Flushes so the write is visible in the current
        unit of work; the surrounding @transactional commits it.
        """
        session = self._require_session()
        merged = await session.merge(entity)
        await session.flush()
        return merged
:::

`_require_session()` es el accesor heredado que devuelve la sesión activa (la transaccional, una vez que `@transactional` la ha intercambiado). `merge` se basa en la clave primaria, así que tanto la primera escritura (apertura) como toda escritura posterior (ingreso, retirada) toman la misma ruta de código sin `IntegrityError`. Para entidades cuyos ids genera la base de datos, `save` es la opción natural; para un agregado que es dueño de su id, lo es `upsert`.

!!! note "Qué acaba de pasar"
    Dos preguntas deciden cada escritura: *¿se confirmó esta fila?* y *¿esta escritura insertó o actualizó?* `@transactional()` responde a la primera (confirma la unidad de trabajo); `upsert`/`merge` responde a la segunda (una sola ruta de código tanto para INSERT como para UPDATE, porque el agregado es dueño de su id). Acierta en ambas y un monedero que abres, ingresas en él y luego lees de vuelta tras un reinicio devuelve el saldo correcto, que es exactamente lo que afirma el test del repositorio de más abajo contra un motor *recién creado*.

### La junta mapeadora agregado ↔ entidad

Hay un límite más, y es una característica, no un accidente. Lumen mantiene dos tipos distintos:

- **`Wallet`** — la *raíz de agregado* de DDD del Capítulo 6. Es dueña del invariante `balance >= 0`, expone métodos que revelan la intención (`open`, `deposit`, `withdraw`) y lanza eventos de dominio. No sabe nada de SQLAlchemy.
- **`WalletEntity`** — la *fila de persistencia*. Es un modelo plano de SQLAlchemy con columnas y sin comportamiento.

Un pequeño mapeador los une, una función pura en cada sentido:

::: listing lumen/core/mappers/wallet_mapper.py | Listado 5.13 — El mapeador agregado ↔ fila
def to_entity(wallet: Wallet) -> WalletEntity:
    """Flatten a Wallet aggregate into a persistable row."""
    assert wallet.id is not None
    return WalletEntity(
        id=wallet.id,
        owner_id=wallet.owner_id,
        currency=wallet.currency.value,
        balance_minor=wallet.balance.amount,
        created_at=wallet.created_at,
    )


def to_aggregate(entity: WalletEntity) -> Wallet:
    """Rehydrate a Wallet aggregate from a persistence row."""
    currency = Currency(entity.currency)
    return Wallet(
        id=entity.id,
        owner_id=entity.owner_id,
        balance=Money(amount=entity.balance_minor, currency=currency),
        created_at=entity.created_at,
    )
:::

El lado de escritura llama a `to_entity` antes del `upsert`; el lado de lectura o bien rehidrata con `to_aggregate` (cuando un comando necesita el agregado rico) o bien proyecta directamente a un DTO (cuando una consulta solo necesita datos). Mantener la fila separada del agregado significa que las preocupaciones de persistencia —tipos de columna, nulabilidad, el baile del merge— nunca se filtran al modelo de dominio, y que los invariantes del dominio nunca limitan el esquema de la tabla. El repositorio almacena filas; el mapeador es la junta que mantiene puro el agregado.

!!! note "La rehidratación se salta la factoría"
    `to_aggregate` llama al **constructor** de `Wallet` directamente, nunca a la factoría `Wallet.open`. La factoría es para monederos *nuevos*: valida las entradas y lanza un evento `WalletOpened`. Una fila cargada desde la base de datos ya representa un monedero válido y confirmado: volver a ejecutar la factoría volvería a disparar ese evento y a comprobar reglas que pasaron hace mucho. El constructor fija los campos en silencio, produciendo un `Wallet` indistinguible de uno recién abierto pero sin eventos espurios.

---

## Activarlo

Activar la capa relacional es configuración, no código: tres claves en `pyfly.yaml`. Añade un bloque `data.relational`:

::: listing pyfly.yaml | Listado 5.14 — Configuración de la capa de datos relacional
pyfly:
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create
:::

`enabled: true` activa la autoconfiguración relacional, que construye el motor asíncrono de SQLAlchemy y el `async_sessionmaker`, registra los beans `AsyncSession` y `session_factory` que inyectan el repositorio y los manejadores, e instala el `RepositoryBeanPostProcessor` que compila tus esbozos de consulta derivada. `url` es una cadena de conexión estándar de SQLAlchemy: SQLite vía `aiosqlite` aquí para un desarrollo de cero infraestructura, `postgresql+asyncpg://…` en producción. `ddl-auto: create` ejecuta `Base.metadata.create_all` al arrancar, así que la tabla `wallets` (descubierta porque `WalletEntity` hereda de `Base`) se construye automáticamente la primera vez que arranca la aplicación.

La huella de dependencias es minúscula: `pyfly[data-relational]` arrastra `sqlalchemy[asyncio]` y `aiosqlite`, y nada más. Sin servidor de base de datos, sin instalación de drivers, que es exactamente por lo que el ejemplo se ejecuta en cualquier sitio.

!!! note "Ejecútalo: el monedero que desaparece, arreglado"
    Vuelve a ejecutar el experimento de la introducción, ahora con la persistencia activada. Abre un monedero, detén la aplicación, arráncala de nuevo y lee el saldo de vuelta:

    ```bash
    # Terminal 1
    uv run pyfly run --server uvicorn

    # Terminal 2 — open a wallet
    curl -s -X POST localhost:8080/api/v1/wallets \
      -H 'content-type: application/json' \
      -d '{"owner_id": "alice", "currency": "EUR"}'
    # -> {"wallet_id": "wlt-..."}

    # Ctrl+C in Terminal 1, then start it again, then:
    curl -s localhost:8080/api/v1/wallets/wlt-.../balance
    ```

    Esta vez el monedero sobrevive: su fila se confirmó en `lumen.db` en disco:

    ```json
    {"id": "wlt-...", "currency": "EUR", "balance_minor": 0, "balance": 0.0}
    ```

    Mira en el directorio del proyecto y verás el fichero SQLite `lumen.db` que el motor creó en el primer arranque, con la tabla `wallets` dentro. Todo el capítulo se reduce a esto: el monedero sobrevive al proceso.

!!! tip "Ciclo de vida del esquema en producción"
    `ddl-auto: create` es lo correcto para desarrollo y ejemplos: crea las tablas que faltan y deja en paz las existentes. En producción fija `ddl-auto: none` y gestiona el esquema con una herramienta de migración (Alembic), que genera scripts versionados a partir del diff entre `Base.metadata` y la base de datos en vivo. El código de la aplicación no cambia: solo el ajuste `ddl-auto` y la canalización de migración.

---

## Demostrar que funciona

Como el repositorio es una clase ordinaria, puedes probarlo directamente contra un fichero SQLite real, sin contexto de aplicación, sin HTTP. El test del repositorio de Lumen crea una base de datos temporal, ejecuta `Base.metadata.create_all` y ejercita la superficie de principio a fin, incluido el `RepositoryBeanPostProcessor` que compila la consulta derivada (el mismo procesador que ejecuta el `ApplicationContext` en vivo):

::: listing lumen/tests/test_sql_wallet_repository.py | Listado 5.15 — Probando el CRUD, la consulta derivada y la ruta de Specification
def _make_repo(session: AsyncSession) -> WalletRepository:
    repo = WalletRepository(WalletEntity, session)
    # Mirror the ApplicationContext: compile derived-query stubs.
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    return repo


@pytest.mark.asyncio
async def test_derived_find_by_owner_id(sqlite_factory) -> None:
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
    sqlite_factory,
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        # age_days drives created_at for newest-first ordering.
        await repo.upsert(_entity("wlt-poor", "a", 50, age_days=3))
        await repo.upsert(_entity("wlt-mid", "b", 1000, age_days=2))
        await repo.upsert(_entity("wlt-rich", "c", 5000, age_days=1))
        await session.commit()

        # balance_minor >= 1000, newest first, page size 1.
        newest_first = Sort.by("created_at").descending()
        page = await repo.find_rich(1000, Pageable.of(1, 1, newest_first))
        assert page.total == 2  # mid + rich
        assert page.total_pages == 2
        assert page.has_next is True
        assert [w.id for w in page.items] == ["wlt-rich"]

        # The bare predicate also works through find_all_by_spec.
        rich = await repo.find_all_by_spec(balance_at_least(5000))
        assert [w.id for w in rich] == ["wlt-rich"]
:::

El primer test ejercita la consulta derivada: tres monederos de entrada, dos propietarios de salida, y `find_by_owner_id("alice")` devuelve exactamente los dos: prueba de que el framework compiló `WHERE owner_id = :owner_id` a partir del nombre del método. El segundo ejercita la ruta de `Specification`: afirma el filtro de umbral (`total == 2`, solo mid y rich cumplen `>= 1000`), la ordenación de más reciente primero (`wlt-rich` es el más reciente de los dos), los metadatos de la página (`total_pages == 2`, `has_next`) y que el mismo predicado `balance_at_least` también se ejecuta sin paginar a través de `find_all_by_spec`.

El fixture refleja lo que hace el framework al arrancar —construir el motor, ejecutar `Base.metadata.create_all` dentro de un bloque `begin()` para que el DDL se confirme, devolver una factoría de sesiones—, de modo que el test ejercita la misma tabla exacta que crea la aplicación. Otros tests del mismo fichero prueban que `upsert` hace un ida y vuelta a través de un motor *recién creado* (durabilidad a través de una reconexión) y que `find_all(pageable)` cuenta y corta correctamente una tabla de cinco monederos.

!!! note "Ejecútalo: demuestra toda la capa en verde"
    Ejecuta el fichero de test del repositorio de principio a fin. Desde la raíz del proyecto Lumen:

    ```bash
    uv run --extra dev pytest tests/test_sql_wallet_repository.py -v
    ```

    Cada test nombrado informa `PASSED`: ida y vuelta del CRUD, durabilidad a través de la reconexión, la consulta derivada, la ruta de especificación y la paginación:

    ```
    tests/test_sql_wallet_repository.py::test_upsert_inserts_then_updates_and_persists PASSED
    tests/test_sql_wallet_repository.py::test_find_by_id_unknown_returns_none PASSED
    tests/test_sql_wallet_repository.py::test_derived_find_by_owner_id PASSED
    tests/test_sql_wallet_repository.py::test_specification_find_rich_paged_and_sorted PASSED
    tests/test_sql_wallet_repository.py::test_find_all_pageable_counts_and_pages PASSED
    6 passed in 0.31s
    ```

    Ejecuta toda la batería (`uv run --extra dev pytest -q`) para confirmar que el resto de Lumen sigue pasando junto a la capa de persistencia.

!!! spring "Equivalencia con Spring"
    Construir el repositorio directamente contra una base de datos real en el mismo proceso refleja el slice `@DataJpaTest` de Spring, que arranca una base de datos H2 y la capa JPA de forma aislada para probar repositorios sin el contexto completo. `Base.metadata.create_all` es el análogo de `spring.jpa.hibernate.ddl-auto=create`, y ejecutar `RepositoryBeanPostProcessor` a mano hace las veces del proxy de Spring que materializa las consultas derivadas sobre un `JpaRepository` al arrancar.

---

## Lo que construiste {.recap}

Lumen ahora persiste los monederos a través de la capa de repositorios al estilo de Spring Data de PyFly:

- **Entidad** — `WalletEntity(Base)`, una fila de SQLAlchemy 2.0 con una clave primaria de cadena (el propio id del agregado) y saldos en unidades menores enteras.
- **Repositorio** — `WalletRepository(Repository[WalletEntity, str])`, marcado con `@repository`. El framework suministra el CRUD asíncrono completo (`save`, `find_by_id`, `find_all`, `delete`/`delete_by_id`, `delete_all`/`delete_all_by_id`, `count`, `exists_by_id`, `save_all`, `find_all_by_id`, `stream_all`, paginación, especificaciones) sin ningún adaptador escrito a mano.
- **Consulta derivada** — `find_by_owner_id`, declarada como un esbozo `...` y compilada a partir de su nombre por el `RepositoryBeanPostProcessor`.
- **Paginación** — `find_all(pageable)` devolviendo un `Page[T]` con `total` / `total_pages` / `has_next`, mapeado a DTOs con `Page.map`, expuesto en `GET /api/v1/wallets`.
- **Especificación** — `balance_at_least(n)` compuesta con `& | ~` y ejecutada vía `find_all_by_spec_paged`, expuesta en `GET /api/v1/wallets/rich`.
- **Proyección** — `@projection BalanceView`, una dataclass concreta sobre la que el `Mapper` proyecta las filas para la vista de lectura del saldo.
- **Transacciones** — manejadores de escritura decorados con `@transactional()` (porque `save`/`upsert` solo *vuelcan*), usando `upsert`/`session.merge` para un agregado que es dueño de su id, con el mapeador agregado ↔ entidad manteniendo puro el modelo de dominio.

Escribiste interfaces y esbozos; el framework escribió el SQL. Esa es la recompensa del patrón Repositorio.

---

## Pruébalo tú mismo {.exercises}

1. **Añade un contador derivado.** Declara `async def count_by_currency(self, currency: str) -> int: ...` en `WalletRepository` (cuerpo `...`). Escribe un test que haga upsert de monederos en dos monedas y afirme el conteo de cada una, confirmando que el prefijo `count_by` compila a `SELECT COUNT(*) … WHERE currency = :currency` sin ningún SQL por tu parte.

2. **Compón dos especificaciones.** Define una segunda factoría `in_currency(code: str) -> Specification[WalletEntity]` (predicado `currency == code`), y luego añade un método de repositorio que ejecute `balance_at_least(min_minor) & in_currency(code)` a través de `find_all_by_spec_paged`. Prueba que devuelve solo los monederos ricos en la moneda elegida, de más reciente primero.

3. **Sigue el rastro del límite transaccional.** Cambia temporalmente `OpenWalletHandler.do_handle` para que llame a `self._repository.save(to_entity(wallet))` en lugar de `upsert`, abre el mismo monedero dos veces en un test y observa el `IntegrityError`. Restaura `upsert`. Luego quita el decorador `@transactional()`, abre un monedero y afirma que **no** sobrevive a una reconexión con motor recién creado, demostrando que sin la confirmación de la unidad de trabajo, el `flush` por sí solo no es durabilidad.

4. **Proyecta una vista diferente.** Añade una dataclass `@projection OwnerView` con solo `id` y `owner_id`, regístrala en un `Mapper` y escribe un test sin manejador que cargue un `WalletEntity` y lo proyecte, verificando que solo se leen las dos columnas declaradas y que se ignora el resto de la fila.
