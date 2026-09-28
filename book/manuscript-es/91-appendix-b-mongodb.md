<span class="eyebrow">Apéndice B</span>

# MongoDB y datos documentales {.chtitle}

La capa de datos documentales de PyFly envuelve MongoDB mediante **Beanie ODM** y la API
asíncrona de PyMongo (**`AsyncMongoClient`**). La API replica deliberadamente la del adaptador relacional: la misma
clase base `MongoRepository[T, ID]`, la misma convención de nombres para consultas derivadas, el mismo
vocabulario `Page`/`Pageable`/`Sort`, de modo que cambiar entre almacenamiento relacional y documental
solo afecta a la definición de la clase de documento y a la clase base del repositorio, no
a la capa de servicio. También se apoya en el mismo modelo de unidad de trabajo: `@transactional`,
las reglas de reversión y las propagaciones funcionan igual (salvo `NESTED`: MongoDB no tiene
puntos de guardado).

Todos los tipos concretos viven en `pyfly.data.document.mongodb`. Los tipos compartidos (`Page`,
`Pageable`, `Sort`) provienen de `pyfly.data`.

---

## Instalación y configuración

Instala el extra:

::: listing terminal | Listado B.1 — Instalar el extra data-document
uv add "pyfly[data-document]"
:::

Habilita el adaptador en `pyfly.yaml`:

::: listing pyfly.yaml | Listado B.2 — Configuración mínima de MongoDB
pyfly:
  data:
    document:
      enabled: true
      uri: "mongodb://localhost:27017"
      database: "myapp"
      min_pool_size: 5
      max_pool_size: 50
:::

### Referencia de configuración

| Clave de `pyfly.yaml` | Tipo | Valor por defecto | Descripción |
|---|---|---|---|
| `pyfly.data.document.enabled` | bool | `false` | Habilita el adaptador de MongoDB |
| `pyfly.data.document.uri` | str | `mongodb://localhost:27017` | URI de conexión |
| `pyfly.data.document.database` | str | `pyfly` | Nombre de la base de datos |
| `pyfly.data.document.min_pool_size` | int | `0` | Mínimo del pool de conexiones (`minPoolSize`) |
| `pyfly.data.document.max_pool_size` | int | `100` | Máximo del pool de conexiones (`maxPoolSize`) |
| `pyfly.data.document.datasource` | str | `document` | Nombre del origen de datos de las unidades de trabajo documentales |
| `pyfly.data.document.tz_aware` | bool | `true` | Las fechas vuelven como valores UTC con zona horaria |
| `pyfly.data.document.transaction.default` | bool | sin fijar | Si el origen documental es el de por defecto de `@transactional` (por defecto, cuando la capa relacional está desactivada) |

Cada clave tiene una variable de entorno equivalente: sustituye los puntos por guiones bajos y
pásala a mayúsculas; por ejemplo, `PYFLY_DATA_DOCUMENT_URI`. Para MongoDB Atlas o un conjunto de réplicas (replica set):

::: listing pyfly.yaml | Listado B.3 — URIs de Atlas y de conjunto de réplicas
# Atlas
pyfly:
  data:
    document:
      enabled: true
      uri: >-
        mongodb+srv://user:secret@cluster.mongodb.net/
        ?retryWrites=true&w=majority
      database: production_db

# Replica set (required for transactions)
# pyfly:
#   data:
#     document:
#       uri: "mongodb://m1:27017,m2:27017,m3:27017/?replicaSet=rs0"
:::

---

## BaseDocument

`BaseDocument` extiende `beanie.Document` con un rastro de auditoría. Toda clase de documento en
una aplicación PyFly debería heredar de ella.

| Campo | Tipo | Valor por defecto | Descripción |
|---|---|---|---|
| `id` | `PydanticObjectId` | Autogenerado | Clave primaria del documento (ObjectId) |
| `created_at` | `datetime` | `datetime.now(UTC)` | Marca de tiempo de inserción |
| `updated_at` | `datetime` | `datetime.now(UTC)` | Marca de tiempo de la última actualización |
| `created_by` | `str \| None` | `None` | Identificador del creador |
| `updated_by` | `str \| None` | `None` | Identificador de quien actualizó por última vez |

`use_state_management = True` se establece en la clase base `Settings`, lo que habilita el
seguimiento de cambios de Beanie para que `save_changes()` produzca actualizaciones parciales eficientes.

Una clase de documento típica:

::: listing catalog/product_document.py | Listado B.4 — ProductDocument con índice y modelo anidado
from pydantic import BaseModel, Field
from beanie import Indexed, PydanticObjectId
from pyfly.data.document.mongodb import BaseDocument


class Dimensions(BaseModel):
    width_cm: float
    height_cm: float
    depth_cm: float


class ProductDocument(BaseDocument):
    name: str
    sku: Indexed(str, unique=True)
    description: str = ""
    price: float = Field(gt=0)
    category: Indexed(str)
    tags: list[str] = Field(default_factory=list)
    dimensions: Dimensions | None = None
    active: bool = True

    class Settings:
        name = "products"
:::

El atributo `Settings.name` establece el nombre de la colección de MongoDB. Si lo omites,
Beanie deriva el nombre a partir del nombre de la clase, lo cual rara vez es lo que deseas.

Para índices compuestos o descendentes usa `Settings.indexes`:

::: listing catalog/product_document.py | Listado B.5 — Índice compuesto mediante Settings.indexes
from pymongo import IndexModel, ASCENDING, DESCENDING
from pyfly.data.document.mongodb import BaseDocument


class OrderDocument(BaseDocument):
    customer_id: str
    status: str
    total: float
    region: str

    class Settings:
        name = "orders"
        indexes = [
            IndexModel(
                [("customer_id", ASCENDING), ("status", ASCENDING)],
                name="idx_customer_status",
            ),
            IndexModel(
                [("region", ASCENDING), ("total", DESCENDING)],
                name="idx_region_total",
            ),
        ]
:::

---

## Correspondencia entre Spring Data y MongoRepository

La siguiente tabla muestra cómo los conceptos de Spring Data MongoDB se corresponden con la capa documental de PyFly.
La superficie es intencionadamente idéntica a la de `Repository[T, ID]` del adaptador relacional,
de modo que los mismos patrones de la capa de servicio se aplican a ambos.

| Spring Data MongoDB | PyFly | Notas |
|---|---|---|
| `MongoRepository<E, ID>` | `MongoRepository[E, ID]` | `from pyfly.data.document.mongodb import MongoRepository`; decora con `@repository`. |
| `@Document class Product` + `@Id` | `class ProductDocument(BaseDocument)` | `from pyfly.data.document.mongodb import BaseDocument`. Hereda `id` (`PydanticObjectId` de Beanie), `created_at`, `updated_at`, `created_by`, `updated_by`. El nombre de la colección se fija en `class Settings: name = "products"`. |
| `findByCategory(String c)` | `async def find_by_category(self, category: str) -> list[ProductDocument]: ...` | El cuerpo vacío `...` lo compila `MongoRepositoryBeanPostProcessor` al arrancar. Mismos prefijos que en el relacional: `find_by_`, `count_by_`, `exists_by_`, `delete_by_`. |
| `@Query("{ 'status': ?0 }")` | `@query('{"status": ":status"}')` | `from pyfly.data.query import query`. Filtro JSON o pipeline de agregación; sustitución de `:param`. |
| `MongoSpecification` | `MongoSpecification(lambda root, q: {"active": True})` | `from pyfly.data.document.mongodb import MongoSpecification`. Compón con `&` / `\|` / `~`; ejecuta mediante `find_all_by_spec(spec)` o `find_all_by_spec_paged(spec, pageable)`. |
| `PageRequest.of(page, size, Sort.by(…))` | `Pageable.of(page, size, Sort.by("name").descending())` | `from pyfly.data import Pageable, Sort`, idéntico al adaptador relacional. |
| `Page<T>` | `Page[T]` | `.items`, `.total`, `.page`, `.size`, `.total_pages`, `.map(fn)`, igual que en el relacional. |

---

## MongoRepository[T, ID]

Crea una subclase de `MongoRepository[T, ID]` y anótala con `@repository`. El framework
extrae el tipo de documento y el tipo de ID a partir de los parámetros genéricos en el momento
de definir la clase, mediante `__init_subclass__`. No se requiere ningún `__init__`.

::: listing catalog/product_repository.py | Listado B.6 — ProductRepository: CRUD + consultas derivadas
from beanie import PydanticObjectId
from pyfly.container import repository
from pyfly.data.document.mongodb import MongoRepository

from catalog.product_document import ProductDocument


@repository
class ProductRepository(MongoRepository[ProductDocument, PydanticObjectId]):

    # --- derived query method stubs (compiled at startup) ---

    async def find_by_category(
        self, category: str
    ) -> list[ProductDocument]: ...

    async def find_by_active_and_category(
        self, active: bool, category: str
    ) -> list[ProductDocument]: ...

    async def find_by_price_greater_than_order_by_price_desc(
        self, min_price: float
    ) -> list[ProductDocument]: ...

    async def find_by_name_containing(
        self, fragment: str
    ) -> list[ProductDocument]: ...

    async def count_by_category(self, category: str) -> int: ...

    async def exists_by_sku(self, sku: str) -> bool: ...

    async def delete_by_active(self, active: bool) -> int: ...
:::

### Métodos CRUD integrados

| Método | Tipo de retorno | Descripción |
|---|---|---|
| `save(entity)` | `T` | Inserta un documento nuevo (`insert_one`) o actualiza uno guardado, en un solo comando |
| `find_by_id(id)` | `T \| None` | Busca por clave primaria |
| `find_all(**filters)` | `list[T]` | Busca todos; los argumentos por palabra clave se convierten en filtros de igualdad |
| `find_all(sort)` | `list[T]` | Recupera todos los documentos, ordenados por un `Sort` |
| `find_all(pageable)` | `Page[T]` | Consulta paginada: cuenta el total, aplica el orden del Pageable, recorta con skip/limit y devuelve `Page[T]` |
| `stream_all(sort)` | `AsyncIterator[T]` | Transmite todos los documentos (el análogo de `Flux<T>`); admite un `Sort` y filtros de igualdad opcionales |
| `delete(entity)` | `None` | Elimina una instancia de documento ya cargada |
| `delete_by_id(id)` | `None` | Elimina por clave primaria; no hace nada si no se encuentra |
| `count()` | `int` | Cuenta todos los documentos de la colección |
| `exists_by_id(id)` | `bool` | True si existe un documento con este ID |
| `save_all(entities)` | `list[T]` | Una escritura masiva ordenada para documentos nuevos y guardados (atómica en un conjunto de réplicas) |
| `find_all_by_id(ids)` | `list[T]` | Busca todos cuyos ID estén en una lista |
| `delete_all_by_id(ids)` | `None` | Elimina todos cuyos ID estén en una lista |
| `delete_all(entities=None)` | `None` | Elimina los documentos indicados; sin argumentos, vacía la colección entera |
| `delete_all_in_batch(entities=None)` | `None` | Borrado masivo que se salta las acciones de evento de borrado |
| `find_all_by_spec(spec)` | `list[T]` | Busca los que coincidan con una `MongoSpecification` |
| `find_all_by_spec_paged(spec, pageable)` | `Page[T]` | Busca los que coincidan con una `MongoSpecification`, con paginación y orden |

`find_all(**filters)` traduce los argumentos por palabra clave en filtros de igualdad de MongoDB:

```python
# {"status": "PENDING", "customer_id": "abc"}
orders = await repo.find_all(status="PENDING", customer_id="abc")
```

El repositorio nunca guarda una sesión. Dentro de una unidad de trabajo de su origen de datos (`@transactional`, una entrega de mensaje) cada llamada pasa la sesión de la unidad al driver, así que sus escrituras forman parte de la transacción. Fuera de ella, cada llamada se ejecuta en una unidad breve propia: una lectura sin transacción, una escritura que envía un solo comando (`save`, `delete`) por su cuenta, y un método que escribe más de una vez (`save_all`) en una transacción que se confirma al final de la llamada. Un guardado fallido devuelve a los documentos el id y la revisión que tenían, así que los mismos objetos se pueden volver a guardar; un documento nuevo se inserta con `insert_one`, así que la lógica de una sobrescritura de `insert`/`save` debe pasar a acciones `@before_event`/`@after_event`.

---

## Métodos de consulta derivados

PyFly compila los métodos vacíos (stubs) de las subclases de `MongoRepository` en consultas reales de MongoDB
al arrancar. La convención de nombres es idéntica a la del adaptador relacional y a la de Spring
Data: `{prefix}_by_{predicates}[_order_by_{fields}]`.

**Prefijos:** `find_by`, `count_by`, `exists_by`, `delete_by`

**Conectores:** `_and_`, `_or_`

### Correspondencia de operadores

| Sufijo del método | Filtro de MongoDB | Argumentos consumidos |
|---|---|---|
| *(ninguno, por defecto)* | `{field: value}` | 1 |
| `_not` | `{field: {"$nin": [value, None]}}` (null nunca coincide, como en SQL) | 1 |
| `_greater_than` | `{field: {"$gt": value}}` | 1 |
| `_greater_than_equal` | `{field: {"$gte": value}}` | 1 |
| `_less_than` | `{field: {"$lt": value}}` | 1 |
| `_less_than_equal` | `{field: {"$lte": value}}` | 1 |
| `_between` | `{field: {"$gte": low, "$lte": high}}` | 2 |
| `_like` | `{field: {"$regex": "^...$"}}` (el `LIKE` de SQL: anclado, distingue mayúsculas) | 1 |
| `_containing` | `{field: {"$regex": "<valor escapado>"}}` (distingue mayúsculas) | 1 |
| `_in` | `{field: {"$in": values}}` | 1 (lista) |
| `_is_null` | `{field: None}` | 0 |
| `_is_not_null` | `{field: {"$ne": None}}` | 0 |

Añade `_ignore_case` tras un predicado para una coincidencia que no distinga mayúsculas. (Antes de la v26.09.08, `_containing` ignoraba las mayúsculas y `_like` no estaba anclado.) Ordenación: añade `_order_by_{field}_{asc|desc}`. Varios campos de ordenación se encadenan:

```python
# sort=[("name", ASC), ("created_at", DESC)]
async def find_by_active_order_by_name_asc_created_at_desc(
    self, active: bool
) -> list[ProductDocument]: ...
```

El `MongoRepositoryBeanPostProcessor` detecta los stubs (cuerpos que solo contienen `...`
o `pass`) y los reemplaza por invocables compilados. Fuente:
`src/pyfly/data/document/mongodb/post_processor.py` y
`src/pyfly/data/document/mongodb/query_compiler.py`.

---

## Consultas personalizadas con @query

Para las consultas que no pueden expresarse mediante convenciones de nombres, `@query` acepta un
documento de filtro de MongoDB (`{…}`) o un pipeline de agregación (`[…]`) como cadena JSON.
Los parámetros con nombre usan la sintaxis `:param_name`.

::: listing catalog/order_repository.py | Listado B.7 — Ejemplos de filtro y agregación con @query
from pyfly.container import repository
from pyfly.data.document.mongodb import MongoRepository
from pyfly.data.query import query

from catalog.order_document import OrderDocument


@repository
class OrderRepository(MongoRepository[OrderDocument, str]):

    @query('{"status": ":status", "total": {"$gte": ":min_total"}}')
    async def find_by_status_min_total(
        self, status: str, min_total: float
    ) -> list[OrderDocument]: ...

    @query(
        '[{"$match": {"customer_id": ":cid"}},'
        ' {"$group": {"_id": "$category",'
        '             "total": {"$sum": "$amount"}}}]'
    )
    async def totals_by_category(
        self, cid: str
    ) -> list[dict]: ...
:::

**Reglas de sustitución:**

- Un valor de cadena JSON que sea *exactamente* `:param_name` se reemplaza por el valor de Python,
  conservando su tipo (`int`, `bool`, `list`, etc.).
- Un `:param_name` incrustado dentro de una cadena mayor se reemplaza mediante `str(value)`.
- Los diccionarios y las listas se recorren recursivamente. Los valores JSON no textuales pasan sin cambios.

`MongoQueryExecutor` analiza la plantilla de la consulta una sola vez al arrancar, detecta si
se trata de un filtro o de un pipeline, y sustituye los parámetros en el momento de la llamada.

---

## Paginación

::: listing catalog/product_service.py | Listado B.8 — Listado de productos paginado
from pyfly.data import Page, Pageable, Sort
from pyfly.data.document.mongodb import MongoRepository

from catalog.product_document import ProductDocument


async def list_products(
    repo: MongoRepository[ProductDocument, str],
    page: int = 1,
    size: int = 20,
) -> Page[ProductDocument]:
    pageable = Pageable.of(
        page=page,
        size=size,
        sort=Sort.by("name"),
    )
    return await repo.find_all(pageable)
:::

`find_all(pageable)` cuenta el total, aplica el orden del Pageable, recorta con
`.skip((page-1)*size)` y `.limit(size)` sobre la consulta de Beanie, y devuelve `Page[T]`.
Pageable parte de 1, así que la página `1` es la primera página.

---

## Gestión de transacciones

Las transacciones multidocumento requieren un despliegue con **conjunto de réplicas** (replica set); basta
con uno de un solo nodo. Una instancia de MongoDB independiente (standalone) no las admite: allí
`@transactional` lanza `IllegalTransactionStateError` en lugar de ejecutarse sin transacción.

::: listing pyfly.yaml | Listado B.9 — Conjunto de réplicas de un solo nodo para desarrollo local
# Start MongoDB: mongod --replSet rs0 --bind_ip localhost
# Init (once, in mongosh): rs.initiate()
pyfly:
  data:
    document:
      enabled: true
      uri: "mongodb://localhost:27017/?replicaSet=rs0"
      database: myapp
:::

MongoDB usa el **mismo** `@transactional` que el adaptador relacional. Su
`MongoTransactionManager` liga una `ClientSession` de pymongo a la tarea en curso, y cada
llamada al repositorio dentro del método se la pasa al driver:

::: listing billing/transfer.py | Listado B.10 — Transferencia de fondos atómica con @transactional
from pyfly.container import service
from pyfly.data import transactional

from billing.account_repository import AccountRepository


@service
class TransferService:
    def __init__(self, accounts: AccountRepository) -> None:
        self._accounts = accounts

    @transactional(datasource="document")
    async def transfer(
        self, from_id: str, to_id: str, amount: float
    ) -> None:
        src = await self._accounts.find_by_id(from_id)
        dst = await self._accounts.find_by_id(to_id)
        if src is None or dst is None or src.balance < amount:
            raise ValueError("Invalid transfer")
        src.balance -= amount
        dst.balance += amount
        await self._accounts.save(src)
        await self._accounts.save(dst)  # a failure here undoes the debit
:::

Si todo va bien, la transacción se confirma (commit); ante cualquier excepción se aborta y se vuelve a lanzar. `datasource="document"`
nombra el origen de datos documental; en una aplicación sin capa relacional es el de por defecto,
y un `@transactional` sin argumentos lo encuentra. Un servicio con un `_session_factory` relacional y
un `_motor_client` a la vez debe nombrar su origen de datos, o la llamada lanza
`IllegalTransactionStateError`. El resto sigue las reglas relacionales: `REQUIRES_NEW` suspende la
unidad, `NESTED` lanza `NestedTransactionNotSupportedError`, solo se acepta `Isolation.DEFAULT`, un
fallo capturado de un participante hace que el commit lance `UnexpectedRollbackError`, y un commit
cuyo resultado el driver no puede conocer lanza `CommitOutcomeUnknownError`. El código que llama
directamente a Beanie o a pymongo pasa la sesión: un parámetro `session` la recibe, y
`current_session()` la devuelve.

El outbox transaccional también funciona sobre MongoDB: con `pyfly.eda.outbox.enabled: true`, una
aplicación solo de MongoDB añade sus eventos a colecciones `pyfly_outbox_*` en la propia transacción
de la unidad (`pyfly.eda.outbox.store: mongo`, que `auto` elige allí).

!!! warning "Se requiere un conjunto de réplicas"
    `@transactional` lanza `IllegalTransactionStateError` contra una instancia de MongoDB independiente.
    Usa el fragmento de URI `?replicaSet=rs0` (consulta el Listado B.9) incluso en desarrollo local. En una
    aplicación solo de MongoDB sobre un servidor independiente, fija además
    `pyfly.messaging.listener.transactional: false`: si no, cada entrega de mensaje abriría una unidad
    documental y fallaría.

!!! note "Motor ya no está"
    Desde la v26.09.08 solo se acepta el `AsyncMongoClient` de PyMongo: el gestor de transacciones
    rechaza un cliente de Motor o de mongomock con `TypeError`. `mongo_transactional` aún se puede
    importar, como alias obsoleto de `@transactional`.

---

## Autoconfiguración

`DocumentAutoConfiguration` se activa cuando:

1. `beanie` es importable (`@conditional_on_class("beanie")`), y
2. `pyfly.data.document.enabled` vale `"true"` en la configuración.

Registra estos beans automáticamente:

| Bean | Tipo | Función |
|---|---|---|
| `mongo_client` | `AsyncMongoClient` | El cliente y su pool de conexiones (tu propio bean singleton `AsyncMongoClient` lo sustituye) |
| `mongo_post_processor` | `MongoRepositoryBeanPostProcessor` | Compila los stubs de consultas derivadas |
| `odm_initializer` | `BeanieInitializer` | Llama a `init_beanie()` al arrancar |
| `mongo_transaction_manager` | `MongoTransactionManager` | Ejecuta las unidades de trabajo documentales |
| `mongo_health_indicator` | `MongoHealthIndicator` | Comprobación de preparación (`ping`, 2 s) |

`BeanieInitializer` descubre por sí mismo las clases de documento: el documento de cada bean
`MongoRepository`, cada documento de Beanie registrado en el contenedor, las clases o módulos
listados en `pyfly.data.document.models`, y los documentos que nombran sus campos `Link`. Esto
significa que definir un repositorio es suficiente: no necesitas registrar los modelos de documento
por separado.

Archivos fuente: `src/pyfly/data/document/auto_configuration.py`,
`src/pyfly/data/document/mongodb/initializer.py`.

---

## Pruebas

Prueba el código transaccional contra un conjunto de réplicas real. `mongodb_replica_set_container()`
arranca en Docker un conjunto de réplicas `rs0` de un solo nodo, y `pyfly_config` activa la capa
documental con su URI:

::: listing tests/test_transfer.py | Listado B.11 — Una prueba de transacción sobre un conjunto de réplicas
import pytest

from pyfly.testing import (
    data_slice,
    mongodb_replica_set_container,
    pyfly_config,
    requires_docker,
)

from billing.account_document import AccountDocument
from billing.account_repository import AccountRepository
from billing.transfer import TransferService


@pytest.fixture(scope="module")
def mongo():
    with mongodb_replica_set_container() as container:
        yield container


@requires_docker
async def test_transfer_commits_both_sides(mongo) -> None:
    config = pyfly_config(
        mongo, base={"pyfly.data.document.database": "billing_test"}
    )
    async with await data_slice(
        AccountRepository, TransferService, config=config
    ) as ctx:
        accounts = ctx.get_bean(AccountRepository)
        transfers = ctx.get_bean(TransferService)
        await accounts.delete_all()
        src = await accounts.save(AccountDocument(balance=100))
        dst = await accounts.save(AccountDocument(balance=0))

        await transfers.transfer(str(src.id), str(dst.id), 60)
        with pytest.raises(ValueError):
            await transfers.transfer(str(src.id), str(dst.id), 60)

        assert (await accounts.find_by_id(src.id)).balance == 40
        assert (await accounts.find_by_id(dst.id)).balance == 60
:::

`@requires_docker` omite la prueba donde Docker no está disponible. La reversión del slice solo
cubre los orígenes de datos relacionales, así que la prueba vacía antes su colección. Instala el
soporte con `pip install 'pyfly[testcontainers]'`; mongomock y Motor ya no se admiten.
