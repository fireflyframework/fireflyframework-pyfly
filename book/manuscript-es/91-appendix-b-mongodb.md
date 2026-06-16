<span class="eyebrow">Apéndice B</span>

# MongoDB y datos documentales {.chtitle}

La capa de datos documentales de PyFly envuelve MongoDB mediante **Beanie ODM** y **Motor** (el
driver asíncrono de MongoDB). La API replica deliberadamente la del adaptador relacional: la misma
clase base `MongoRepository[T, ID]`, la misma convención de nombres para consultas derivadas, el mismo
vocabulario `Page`/`Pageable`/`Sort`, de modo que cambiar entre almacenamiento relacional y documental
solo afecta a la definición de la clase de documento y a la clase base del repositorio, no
a la capa de servicio.

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
| `pyfly.data.document.min_pool_size` | int | `0` | Mínimo del pool de Motor |
| `pyfly.data.document.max_pool_size` | int | `100` | Máximo del pool de Motor |

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
| `save(entity)` | `T` | Inserta o actualiza mediante `entity.save()` de Beanie |
| `find_by_id(id)` | `T \| None` | Busca por clave primaria |
| `find_all(**filters)` | `list[T]` | Busca todos; los argumentos por palabra clave se convierten en filtros de igualdad |
| `find_all(sort)` | `list[T]` | Recupera todos los documentos, ordenados por un `Sort` |
| `find_all(pageable)` | `Page[T]` | Consulta paginada: cuenta el total, aplica el orden del Pageable, recorta con skip/limit y devuelve `Page[T]` |
| `stream_all(sort)` | `AsyncIterator[T]` | Transmite todos los documentos (el análogo de Flux<T>); admite un `Sort` y filtros de igualdad opcionales |
| `delete(entity)` | `None` | Elimina una instancia de documento ya cargada |
| `delete_by_id(id)` | `None` | Elimina por clave primaria; no hace nada si no se encuentra |
| `count()` | `int` | Cuenta todos los documentos de la colección |
| `exists_by_id(id)` | `bool` | True si existe un documento con este ID |
| `save_all(entities)` | `list[T]` | Inserción masiva mediante `insert_many` |
| `find_all_by_id(ids)` | `list[T]` | Busca todos cuyos ID estén en una lista |
| `delete_all_by_id(ids)` | `None` | Elimina todos cuyos ID estén en una lista |
| `delete_all(entities=None)` | `None` | Elimina los documentos indicados; sin argumentos, vacía la colección entera |
| `find_all_by_spec(spec)` | `list[T]` | Busca los que coincidan con una `MongoSpecification` |
| `find_all_by_spec_paged(spec, pageable)` | `Page[T]` | Busca los que coincidan con una `MongoSpecification`, con paginación y orden |

`find_all(**filters)` traduce los argumentos por palabra clave en filtros de igualdad de MongoDB:

```python
# {"status": "PENDING", "customer_id": "abc"}
orders = await repo.find_all(status="PENDING", customer_id="abc")
```

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
| `_not` | `{field: {"$ne": value}}` | 1 |
| `_greater_than` | `{field: {"$gt": value}}` | 1 |
| `_greater_than_equal` | `{field: {"$gte": value}}` | 1 |
| `_less_than` | `{field: {"$lt": value}}` | 1 |
| `_less_than_equal` | `{field: {"$lte": value}}` | 1 |
| `_between` | `{field: {"$gte": low, "$lte": high}}` | 2 |
| `_like` | `{field: {"$regex": pattern}}` (SQL % → .*) | 1 |
| `_containing` | `{field: {"$regex": ".*val.*", "$options": "i"}}` | 1 |
| `_in` | `{field: {"$in": values}}` | 1 (lista) |
| `_is_null` | `{field: None}` | 0 |
| `_is_not_null` | `{field: {"$ne": None}}` | 0 |

Ordenación: añade `_order_by_{field}_{asc|desc}`. Varios campos de ordenación se encadenan:

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

Las transacciones multidocumento requieren un despliegue con **conjunto de réplicas** (replica set). Una instancia
de MongoDB independiente (standalone) no las admite.

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

El decorador `@mongo_transactional` envuelve una función asíncrona en una sesión y una
transacción de Motor. Las operaciones de Beanie de la función participan automáticamente:

::: listing billing/transfer.py | Listado B.10 — Transferencia de fondos atómica con @mongo_transactional
from motor.motor_asyncio import AsyncIOMotorClient
from pyfly.data.document.mongodb import mongo_transactional

from billing.account_document import AccountDocument


def make_transfer_fn(client: AsyncIOMotorClient):
    @mongo_transactional(client)
    async def transfer(
        from_id: str, to_id: str, amount: float
    ) -> None:
        src = await AccountDocument.get(from_id)
        dst = await AccountDocument.get(to_id)
        if src is None or dst is None or src.balance < amount:
            raise ValueError("Invalid transfer")
        src.balance -= amount
        dst.balance += amount
        await src.save()
        await dst.save()
    return transfer
:::

Si todo va bien, la transacción se confirma (commit); ante cualquier excepción se aborta y se vuelve a lanzar. A diferencia
de `@reactive_transactional` del relacional, la sesión de Motor no se inyecta como
argumento: Beanie la recoge a través del contexto de Motor.

El bean `motor_client` lo registra automáticamente `DocumentAutoConfiguration`
cuando el adaptador está habilitado. Inyéctalo en tu servicio mediante el contenedor de inyección de dependencias.

!!! warning "Se requiere un conjunto de réplicas"
    `@mongo_transactional` lanzará un error contra una instancia de MongoDB independiente.
    Usa el fragmento de URI `?replicaSet=rs0` (consulta el Listado B.9) incluso en desarrollo local.

---

## Autoconfiguración

`DocumentAutoConfiguration` se activa cuando:

1. `beanie` es importable (`@conditional_on_class("beanie")`), y
2. `pyfly.data.document.enabled` vale `"true"` en la configuración.

Registra tres beans automáticamente:

| Bean | Tipo | Función |
|---|---|---|
| `motor_client` | `AsyncIOMotorClient` | Pool de conexiones asíncrono de MongoDB |
| `mongo_post_processor` | `MongoRepositoryBeanPostProcessor` | Compila los stubs de consultas derivadas |
| `odm_initializer` | `BeanieInitializer` | Llama a `init_beanie()` al arrancar |

`BeanieInitializer.start()` descubre las subclases de `BaseDocument` en dos pasadas: primero
desde cada `MongoRepository._entity_type` registrado (establecido por `__init_subclass__`),
y luego las subclases de `BaseDocument` registradas directamente. Esto significa que definir un repositorio
es suficiente: no necesitas registrar los modelos de documento por separado.

Archivos fuente: `src/pyfly/data/document/auto_configuration.py`,
`src/pyfly/data/document/mongodb/initializer.py`.

---

## Pruebas

Para las pruebas unitarias, usa [mongomock-motor](https://github.com/michaelkryukov/mongomock-motor)
o apunta a una base de datos de pruebas dedicada:

::: listing pyfly-test.yaml | Listado B.11 — Configuración de la base de datos de pruebas
pyfly:
  data:
    document:
      enabled: true
      database: "myapp_test"
:::

Para las pruebas de integración, el soporte de Testcontainers de PyFly levanta automáticamente un contenedor
real de MongoDB; consulta el capítulo de pruebas y
`@ServiceConnection(MongoDBContainer)`.
