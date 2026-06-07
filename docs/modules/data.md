# Data Module

> **Package:** `pyfly.data`
> **Role:** Framework-agnostic commons layer — shared abstractions for all data adapters.
>
> PyFly Data follows the **Spring Data umbrella architecture**: a single commons module defines the contracts (ports, pagination, query parsing, mapping), and pluggable adapters provide backend-specific implementations. Your service layer depends on these commons types — never on an adapter directly.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
  - [Two-Layer Design](#two-layer-design)
  - [Package Mapping](#package-mapping)
  - [Import Rules](#import-rules)
- [Repository Ports](#repository-ports)
  - [RepositoryPort\[T, ID\]](#repositoryportt-id)
  - [SessionPort](#sessionport)
  - [CrudRepository\[T, ID\]](#crudrepositoryt-id)
  - [PagingRepository\[T, ID\]](#pagingrepositoryt-id)
  - [Hexagonal Usage Pattern](#hexagonal-usage-pattern)
- [Derived Query Methods](#derived-query-methods)
  - [QueryMethodParser](#querymethodparser)
  - [Naming Convention](#naming-convention)
  - [Prefixes](#prefixes)
  - [Operators](#operators)
  - [Connectors](#connectors)
  - [Ordering](#ordering)
  - [ParsedQuery Dataclass](#parsedquery-dataclass)
  - [QueryMethodCompilerPort](#querymethodcompilerport)
  - [Complete Derived Query Examples](#complete-derived-query-examples)
- [Pagination & Sorting](#pagination--sorting)
  - [Pageable](#pageable)
  - [Sort and Order](#sort-and-order)
  - [Page\[T\]](#paget)
- [Entity Mapping](#entity-mapping)
  - [Basic Mapping](#basic-mapping)
  - [Custom Field Mapping](#custom-field-mapping)
  - [Transformers](#transformers)
  - [Excluding Fields](#excluding-fields)
  - [Mapping Lists](#mapping-lists)
  - [Nested Models & Collections](#nested-models--collections)
  - [Declarative Mapping with @mapping](#declarative-mapping-with-mapping)
  - [add\_mapping() Reference](#add_mapping-reference)
- [Projections](#projections)
  - [@projection Decorator](#projection-decorator)
  - [Projection Utilities](#projection-utilities)
  - [Mapper.register\_projection() and Mapper.project()](#mapperregister_projection-and-mapperproject)
  - [Query Compiler Integration](#query-compiler-integration)
- [Specification Port](#specification-port)
- [BaseFilterUtils Port](#basefilterutils-port)
- [BaseRepositoryPostProcessor Port](#baserepositorypostprocessor-port)
- [Extending PyFly Data](#extending-pyfly-data)
  - [How to Create a Custom Adapter](#how-to-create-a-custom-adapter)
  - [QueryMethodCompilerPort Contract](#querymethodcompilerport-contract)
  - [BeanPostProcessor Pattern](#beanpostprocessor-pattern)
- [Available Adapters](#available-adapters)
- [See Also](#see-also)

---

## Architecture Overview

### Two-Layer Design

The data module follows a hexagonal architecture with two distinct layers:

```
┌──────────────────────────────────────────────────┐
│              Your Application                     │
│   (Services, Controllers, Domain Logic)           │
│                                                   │
│   Depends on:  pyfly.data  (ports only)           │
└──────────────────────┬───────────────────────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
┌──────────────────┐   ┌───────────────────────┐
│   pyfly.data     │   │   pyfly.data           │
│    (Commons)     │   │    (Commons)            │
│                  │   │                         │
│  RepositoryPort  │   │  RepositoryPort         │
│  Page, Pageable  │   │  Page, Pageable         │
│  QueryMethod-    │   │  QueryMethod-           │
│    Parser        │   │    Parser               │
│  QueryMethod-    │   │  QueryMethod-           │
│    CompilerPort  │   │    CompilerPort         │
└────────┬─────────┘   └───────────┬─────────────┘
         │                         │
         ▼                         ▼
┌──────────────────┐   ┌───────────────────────┐
│  pyfly.data      │   │  pyfly.data            │
│   .relational    │   │   .document            │
│   .sqlalchemy    │   │   .mongodb             │
│                  │   │                         │
│  Repository[T]   │   │  MongoRepository[T]     │
│  BaseEntity      │   │  BaseDocument           │
│  QueryMethod-    │   │  MongoQueryMethod-       │
│    Compiler      │   │    Compiler             │
│  reactive_       │   │  mongo_                 │
│    transactional │   │    transactional        │
└────────┬─────────┘   └───────────┬─────────────┘
         │                         │
         ▼                         ▼
┌──────────────────┐   ┌───────────────────────┐
│  SQLAlchemy      │   │  Beanie ODM + Motor    │
│  (async)         │   │  (async)               │
└──────────────────┘   └───────────────────────┘
```

**Layer 1 — Data Commons (`pyfly.data`):** Framework-agnostic types shared by **all** data adapters. These contain zero backend-specific code. Your service layer should depend on these ports.

**Layer 2 — Adapters:** Each adapter provides concrete implementations for a specific database backend. The adapter translates commons-layer contracts into backend-specific operations.

### Package Mapping

| Spring Data Module   | PyFly Equivalent                     | Purpose                                         |
|----------------------|--------------------------------------|-------------------------------------------------|
| Spring Data Commons  | `pyfly.data`                         | Shared ports, types, parser, `Page`, `Sort`     |
| Spring Data JPA      | `pyfly.data.relational.sqlalchemy`   | Relational database adapter (SQLAlchemy)        |
| Spring Data MongoDB  | `pyfly.data.document.mongodb`        | Document database adapter (Beanie/Motor)        |

### Import Rules

**Commons layer** (framework-agnostic — use in your service layer):

```python
from pyfly.data import (
    Page, Pageable, Sort, Order,        # Pagination
    Mapper,                              # Entity ↔ DTO mapping
    RepositoryPort, SessionPort,         # Port interfaces
    CrudRepository, PagingRepository,    # Extended port interfaces
    QueryMethodParser,                   # Derived query parsing (shared)
    QueryMethodCompilerPort,             # Compiler contract
    Specification,                       # Composable query predicate ABC
    BaseFilterUtils,                     # Query by Example ABC
    BaseRepositoryPostProcessor,         # BeanPostProcessor ABC
    DERIVED_PREFIXES,                    # ("find_by_", "count_by_", ...)
)

from pyfly.data.projection import (     # Projection utilities
    projection,                          # @projection decorator
    is_projection,                       # Check if type is a projection
    projection_fields,                   # Get projection field names
)
```

**Adapter layer** (import only in repository/configuration code):

```python
# SQLAlchemy adapter
from pyfly.data.relational.sqlalchemy import Repository, BaseEntity, ...

# MongoDB adapter
from pyfly.data.document.mongodb import MongoRepository, BaseDocument, ...
```

> **Rule of thumb:** Services import from `pyfly.data`. Repositories and configuration import from the adapter package. This keeps your business logic database-agnostic.

Source file: `src/pyfly/data/__init__.py`

---

## Repository Ports

For hexagonal architecture, your service layer should depend on port protocols rather than concrete repository classes.

### RepositoryPort[T, ID]

The base repository interface — a `Protocol` that all adapters satisfy:

```python
class RepositoryPort(Protocol[T, ID]):
    async def save(self, entity: T) -> T: ...
    async def find_by_id(self, id: ID) -> T | None: ...
    async def find_all(self, **filters: Any) -> list[T]: ...
    async def delete(self, id: ID) -> None: ...
    async def count(self) -> int: ...
    async def exists(self, id: ID) -> bool: ...
```

| Method                  | Return Type  | Description                                    |
|-------------------------|--------------|------------------------------------------------|
| `save(entity)`          | `T`          | Insert or update; return persisted entity      |
| `find_by_id(id)`        | `T \| None`  | Find by primary key                            |
| `find_all(**filters)`   | `list[T]`    | Find all, optionally filtered by field values  |
| `delete(id)`            | `None`       | Delete by primary key (no-op if not found)     |
| `count()`               | `int`        | Count all entities                             |
| `exists(id)`            | `bool`       | Check if an entity with this ID exists         |

### SessionPort

Abstract session interface for transaction management:

```python
class SessionPort(Protocol):
    async def begin(self) -> Any: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
```

### CrudRepository[T, ID]

Spring Data-style CRUD interface with type parameters for both entity and ID:

```python
class CrudRepository(Protocol[T, ID]):
    async def save(self, entity: T) -> T: ...
    async def find_by_id(self, id: ID) -> T | None: ...
    async def find_all(self) -> list[T]: ...
    async def delete(self, entity: T) -> None: ...
    async def delete_by_id(self, id: ID) -> None: ...
    async def count(self) -> int: ...
    async def exists_by_id(self, id: ID) -> bool: ...
```

### PagingRepository[T, ID]

Extends `CrudRepository` with pagination:

```python
class PagingRepository(CrudRepository[T, ID], Protocol[T, ID]):
    async def find_all_paged(
        self, page: int = 1, size: int = 20, sort: list[str] | None = None
    ) -> Page[T]: ...
```

### Hexagonal Usage Pattern

```python
from pyfly.data import RepositoryPort


class ProductService:
    def __init__(self, repo: RepositoryPort[Product, str]) -> None:
        self._repo = repo

    async def find_active(self) -> list[Product]:
        return await self._repo.find_all(active=True)
```

The same service works with the SQLAlchemy `Repository`, MongoDB `MongoRepository`, or any future adapter — without any code changes.

Source file: `src/pyfly/data/ports/outbound.py`

---

## Derived Query Methods

PyFly can automatically generate query implementations from method names, following the Spring Data naming convention. You define stub methods on your repository and a `BeanPostProcessor` compiles them into real queries at startup.

### QueryMethodParser

The `QueryMethodParser` lives in the commons layer and is shared by all adapters. It parses method names into structured `ParsedQuery` objects that are backend-agnostic.

```python
from pyfly.data import QueryMethodParser

parser = QueryMethodParser()
parsed = parser.parse("find_by_status_and_role_order_by_name_desc")
# -> ParsedQuery(
#      prefix="find_by",
#      predicates=[FieldPredicate("status", "eq"), FieldPredicate("role", "eq")],
#      connectors=["and"],
#      order_clauses=[OrderClause("name", "desc")]
#    )
```

### Naming Convention

```
<prefix>_<field>[_<operator>][_<connector>_<field>[_<operator>]]*[_order_by_<field>_<direction>]*
```

### Prefixes

| Prefix       | Return Type | Description                            |
|--------------|-------------|----------------------------------------|
| `find_by_`   | `list[T]`   | Find all matching entities             |
| `count_by_`  | `int`       | Count matching entities                |
| `exists_by_` | `bool`      | Check if any entity matches            |
| `delete_by_` | `int`       | Delete matching entities (return count)|

### Operators

Operators are suffixed to field names. They are checked longest-first to avoid partial matches (e.g., `_greater_than_equal` before `_greater_than`).

| Suffix                 | Operator      | Meaning              | Args  |
|------------------------|---------------|----------------------|-------|
| *(none)*               | `eq`          | equals               | 1     |
| `_greater_than`        | `gt`          | `>`                  | 1     |
| `_less_than`           | `lt`          | `<`                  | 1     |
| `_greater_than_equal`  | `gte`         | `>=`                 | 1     |
| `_less_than_equal`     | `lte`         | `<=`                 | 1     |
| `_between`             | `between`     | `BETWEEN ? AND ?`    | 2     |
| `_like`                | `like`        | `LIKE ?`             | 1     |
| `_containing`          | `containing`  | contains substring   | 1     |
| `_in`                  | `in`          | `IN (?)`             | 1 (list) |
| `_not`                 | `not`         | `!=`                 | 1     |
| `_is_null`             | `is_null`     | `IS NULL`            | 0     |
| `_is_not_null`         | `is_not_null` | `IS NOT NULL`        | 0     |

### Connectors

Connect multiple predicates with `_and_` or `_or_`:

```python
# AND: status = ? AND customer_id = ?
async def find_by_status_and_customer_id(self, status: str, customer_id: str) -> list[T]: ...

# OR: status = ? OR role = ?
async def find_by_status_or_role(self, status: str, role: str) -> list[T]: ...
```

### Ordering

Append `_order_by_{field}_{asc|desc}` to control result ordering. Multiple sort fields can be chained:

```python
# ORDER BY created_at DESC
async def find_by_status_order_by_created_at_desc(self, status: str) -> list[T]: ...

# ORDER BY name ASC, created_at DESC
async def find_by_active_order_by_name_asc_created_at_desc(self, active: bool) -> list[T]: ...
```

### ParsedQuery Dataclass

```python
@dataclass
class ParsedQuery:
    prefix: str                          # "find_by", "count_by", "exists_by", "delete_by"
    predicates: list[FieldPredicate]     # [{field_name: "status", operator: "eq"}, ...]
    connectors: list[str]                # ["and", "or", ...]
    order_clauses: list[OrderClause]     # [{field_name: "name", direction: "desc"}, ...]
```

The parsing algorithm:
1. Extracts the prefix (`find_by_`, `count_by_`, etc.).
2. Splits off the `_order_by_` suffix.
3. Splits the remaining body by `_and_` and `_or_` connectors.
4. Parses each segment for field name and operator suffix (longest-match).

### QueryMethodCompilerPort

The `QueryMethodCompilerPort` is the adapter extension point. Each adapter provides its own compiler that translates `ParsedQuery` objects into backend-specific executable queries:

```python
class QueryMethodCompilerPort(Protocol):
    def compile(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, Any]]: ...
```

The parser is fully shared — you never need to reimplement parsing logic. Your adapter only needs to compile parsed queries into the target database's query format.

| Adapter    | Compiler Class             | Output                           |
|------------|---------------------------|----------------------------------|
| SQLAlchemy | `QueryMethodCompiler`      | SQLAlchemy column expressions    |
| MongoDB    | `MongoQueryMethodCompiler` | MongoDB filter documents         |

### Complete Derived Query Examples

```python
# These stubs work with ANY adapter — SQLAlchemy, MongoDB, or custom.
# The naming convention is identical across all PyFly data adapters.

# Equals (default operator)
async def find_by_status(self, status: str) -> list[T]: ...

# Multiple conditions with AND
async def find_by_customer_id_and_status(
    self, customer_id: str, status: str
) -> list[T]: ...

# Greater than
async def find_by_total_greater_than(self, min_total: float) -> list[T]: ...

# Between (takes 2 arguments)
async def find_by_total_between(self, low: float, high: float) -> list[T]: ...

# LIKE pattern
async def find_by_customer_id_like(self, pattern: str) -> list[T]: ...

# Contains (wraps value)
async def find_by_customer_id_containing(self, fragment: str) -> list[T]: ...

# IN a list
async def find_by_status_in(self, statuses: list[str]) -> list[T]: ...

# IS NULL / IS NOT NULL (zero arguments consumed)
async def find_by_deleted_at_is_null(self) -> list[T]: ...
async def find_by_email_is_not_null(self) -> list[T]: ...

# COUNT prefix
async def count_by_status(self, status: str) -> int: ...

# EXISTS prefix
async def exists_by_customer_id(self, customer_id: str) -> bool: ...

# DELETE prefix (returns number of rows deleted)
async def delete_by_status(self, status: str) -> int: ...

# With ordering
async def find_by_status_order_by_created_at_desc(
    self, status: str
) -> list[T]: ...

# Complex: AND + ordering
async def find_by_status_and_customer_id_order_by_total_desc(
    self, status: str, customer_id: str
) -> list[T]: ...
```

Each method body should be a stub (`...` or `pass`). The adapter's `BeanPostProcessor` detects them and replaces them with real implementations at startup.

Source files:
- `src/pyfly/data/query_parser.py` — `QueryMethodParser`, `ParsedQuery`, `FieldPredicate`, `OrderClause`
- `src/pyfly/data/ports/compiler.py` — `QueryMethodCompilerPort`

---

## Pagination & Sorting

### Pageable

`Pageable` is a frozen dataclass that encapsulates pagination request parameters:

```python
from pyfly.data import Pageable, Sort, Order as SortOrder

# Simple pagination
pageable = Pageable.of(page=1, size=20)

# With sorting
pageable = Pageable.of(page=1, size=20, sort=Sort.by("created_at").descending())

# Unpaged (fetch all results)
pageable = Pageable.unpaged()
```

**Fields and properties:**

| Field/Property | Type    | Description                                         |
|----------------|---------|-----------------------------------------------------|
| `page`         | `int`   | Page number (1-based, must be >= 1)                  |
| `size`         | `int`   | Maximum items per page (must be >= 1)                |
| `sort`         | `Sort`  | Sort criteria                                        |
| `offset`       | `int`   | Calculated offset: `(page - 1) * size`               |
| `is_paged`     | `bool`  | `True` for normal pagination, `False` for unpaged    |

**Navigation methods:**

```python
next_page = pageable.next()        # Pageable for page + 1
prev_page = pageable.previous()    # Pageable for page - 1 (minimum page 1)
```

**Validation:** `Pageable.__post_init__` raises `ValueError` if `page < 1` or `size < 1` (except for the unpaged sentinel).

### Sort and Order

`Sort` is a collection of `Order` objects:

```python
from pyfly.data import Sort, Order as SortOrder

# Sort by a single field ascending
sort = Sort.by("name")

# Sort by a single field descending
sort = Sort.by("name").descending()

# Multiple sort fields
sort = Sort(orders=(
    SortOrder.desc("created_at"),
    SortOrder.asc("name"),
))

# Combine sorts
sort1 = Sort.by("name")
sort2 = Sort.by("created_at").descending()
combined = sort1.and_then(sort2)

# No sorting
sort = Sort.unsorted()

# Flip all directions
reversed_sort = sort.descending()  # All orders become desc
```

`Order` is a single sort directive:

```python
order_asc = SortOrder.asc("name")       # Order(property="name", direction="asc")
order_desc = SortOrder.desc("created_at") # Order(property="created_at", direction="desc")
```

### Page[T]

`Page[T]` is a frozen dataclass returned by paginated queries:

```python
page = await repo.find_paginated(page=1, size=20)

page.items          # list[T] -- the items on this page
page.total          # int -- total items across all pages
page.page           # int -- current page number (1-based)
page.size           # int -- maximum items per page
page.total_pages    # int -- total number of pages (ceil(total / size))
page.has_next       # bool -- whether there is a next page
page.has_previous   # bool -- whether there is a previous page
```

**Transforming items:**

The `map()` method transforms each item while preserving pagination metadata:

```python
dto_page: Page[OrderDTO] = page.map(
    lambda order: OrderDTO(id=str(order.id), status=order.status)
)
```

Source files:
- `src/pyfly/data/pageable.py` — `Pageable`, `Sort`, `Order`
- `src/pyfly/data/page.py` — `Page[T]`

---

## Entity Mapping

The `Mapper` class provides runtime, reflection-based type-to-type mapping between entities and DTOs, inspired by MapStruct. It automatically matches fields by name and supports custom renaming, transformers, and exclusion. Unlike a flat field copy, it also **recurses into nested models and collections of models** and is **Pydantic-aware**.

This is the *runtime* equivalent of MapStruct — there is intentionally no compile-time codegen, no generated `*Impl` classes, and no string expression DSL.

`Mapper` works with both **dataclasses** and **Pydantic v2 `BaseModel`** types, in any combination (dataclass → Pydantic, Pydantic → dataclass, etc.). A type is "mappable" when it is a dataclass or a Pydantic v2 model (it has `model_fields` and `model_validate`).

### Basic Mapping

```python
from pyfly.data import Mapper
from dataclasses import dataclass


@dataclass
class OrderDTO:
    id: str
    status: str
    total: float


mapper = Mapper()
dto = mapper.map(order_entity, OrderDTO)
# Matches fields by name: id, status, total
```

Pydantic models work the same way — `map()` constructs the destination through its normal constructor, so a Pydantic destination is fully validated:

```python
from pydantic import BaseModel


class UserEntity(BaseModel):
    username: str
    email: str


class UserDTO(BaseModel):
    username: str
    email: str


dto = Mapper().map(UserEntity(username="ada", email="ada@example.com"), UserDTO)
# UserDTO(username='ada', email='ada@example.com') — validated by Pydantic
```

### Custom Field Mapping

When source and destination field names differ:

```python
mapper = Mapper()
mapper.add_mapping(
    Order, OrderDTO,
    field_map={"customer_id": "buyer_id"},
    # Source field "customer_id" maps to destination field "buyer_id"
)
dto = mapper.map(order, OrderDTO)
```

The `field_map` uses `{source_name: dest_name}` format. Reverse lookup is performed: for each destination field, the mapper checks if any source field maps to it.

### Transformers

Apply functions to transform field values during mapping:

```python
mapper.add_mapping(
    Order, OrderDTO,
    transformers={
        "status": str.upper,        # "pending" -> "PENDING"
        "total": lambda v: round(v, 2),
    },
)
```

Transformers are keyed by destination field name and applied after the value is retrieved from the source.

### Excluding Fields

Omit specific fields from the mapping:

```python
mapper.add_mapping(
    Order, OrderDTO,
    exclude={"internal_notes", "audit_log"},
)
```

### Mapping Lists

```python
dtos = mapper.map_list(orders, OrderDTO)
# Equivalent to [mapper.map(o, OrderDTO) for o in orders]
```

### Nested Models & Collections

`map()` does not just copy top-level fields — when a destination field's declared type is itself mappable, the mapper **recurses** into it. This works for a nested model field and for a collection (`list`, `set`, `frozenset`, `tuple`) of mappable elements. `Optional[X]` / `X | None` is unwrapped to its single non-`None` arg before recursing.

```python
from pydantic import BaseModel
from pyfly.data import Mapper


class AddressEntity(BaseModel):
    street: str
    city: str


class UserEntity(BaseModel):
    username: str
    address: AddressEntity
    tags: list[str] = []


class AddressDTO(BaseModel):
    street: str
    city: str


class UserDTO(BaseModel):
    username: str
    address: AddressDTO
    tags: list[str] = []


src = UserEntity(username="ada", address=AddressEntity(street="1 Main", city="London"), tags=["x"])
dto = Mapper().map(src, UserDTO)

isinstance(dto.address, AddressDTO)   # True — recursed, not left as AddressEntity or a dict
dto.address.city                       # "London"
dto.tags                               # ["x"] — a plain (non-mappable) list is copied as-is
```

Recursion also descends into collections of models, including models nested *within* collection elements:

```python
class TeamEntity(BaseModel):
    name: str
    members: list[UserEntity] = []


class TeamDTO(BaseModel):
    name: str
    members: list[UserDTO] = []


team = TeamEntity(name="A", members=[UserEntity(username="ada", address=AddressEntity(street="s", city="c"))])
dto = Mapper().map(team, TeamDTO)

isinstance(dto.members[0], UserDTO)            # True — each list element is mapped
isinstance(dto.members[0].address, AddressDTO) # True — nested-within-collection also recursed
```

Recursion notes:

- **Pydantic-aware extraction.** Field extraction is *shallow* — nested models stay as live instances rather than being flattened to dicts (a deep `dataclasses.asdict()` would break nested-destination recursion).
- **Already-correct types are left alone.** If a value is already an instance of the destination field's type, or is a primitive, or the destination type is not mappable, the value passes through unchanged.
- **Transformers take precedence.** A field with an explicit `transformers` entry is transformed by that callable; recursion only applies to fields without one.

### Declarative Mapping with @mapping

Instead of imperative `add_mapping()` calls, the `@mapping` class decorator lets a mapping's configuration live next to the involved types. It registers the mapping on a shared module-level `default_mapper` instance:

```python
from pyfly.data import default_mapper, mapping
from pydantic import BaseModel


class UserEntity(BaseModel):
    username: str
    email: str


class UserResponse(BaseModel):
    name: str
    email: str


@mapping(UserEntity, UserResponse, rename={"username": "name"}, transform={"email": str.lower})
class UserMapper:
    pass


resp = default_mapper.map(UserEntity(username="Ada", email="A@B.COM"), UserResponse)
resp.name    # "Ada"  (renamed from username)
resp.email   # "a@b.com"  (lower-cased by the transform)
```

The decorator keyword arguments map onto `add_mapping()` parameters:

| `@mapping` argument | `add_mapping()` parameter | Format |
|---------------------|---------------------------|--------|
| `rename` | `field_map` | `{source_field: dest_field}` |
| `transform` | `transformers` | `{dest_field: transform_fn}` |
| `exclude` | `exclude` | `set[str]` of destination fields to skip |

The decorated class is returned unchanged with a `__pyfly_mapping__ = (source_type, dest_type)` marker attribute attached, and the registration is added to `default_mapper` as an import side effect — so importing the module containing the decorated class is enough to register the mapping. Use `default_mapper.map(...)` / `default_mapper.map_list(...)` to apply it. For mappings scoped to one component, prefer a local `Mapper()` with explicit `add_mapping()` calls instead.

### add_mapping() Reference

| Parameter      | Type                               | Description                                    |
|----------------|------------------------------------|------------------------------------------------|
| `source_type`  | `type[S]`                          | Source class to map from                        |
| `dest_type`    | `type[D]`                          | Destination class to map to                     |
| `field_map`    | `dict[str, str] \| None`           | `{source_field: dest_field}` renaming           |
| `transformers` | `dict[str, Callable] \| None`      | `{dest_field: transform_fn}` value transformers |
| `exclude`      | `set[str] \| None`                 | Destination fields to skip                      |

The mapper supports dataclasses, Pydantic v2 models, and plain objects. Source field extraction is *shallow* (it keeps nested models as live instances): `dataclasses.fields()` for dataclasses, `model_fields` for Pydantic models, and `vars()` for other objects. Destination field discovery uses `dataclasses.fields()`, `model_fields`, or `get_type_hints()`, and the destination's declared field types drive nested-model recursion.

Source file: `src/pyfly/data/mapper.py`

---

## Projections

Projections let you define a **subset of entity fields** as a Protocol type. Query compilers use projections to select only the required columns/fields from the database, and `Mapper.project()` maps full entities to projection types with optional computed fields.

### @projection Decorator

Mark a `Protocol` class as a projection interface:

```python
from typing import Protocol
from pyfly.data.projection import projection


@projection
class OrderSummary(Protocol):
    id: str
    status: str
    total: float
```

The `@projection` decorator adds an internal marker that query compilers and `Mapper` use to identify projection types. The Protocol declares only the fields you need — the query compiler will select just those columns.

### Projection Utilities

Two introspection functions are available:

```python
from pyfly.data.projection import is_projection, projection_fields

is_projection(OrderSummary)       # True
is_projection(OrderDTO)           # False (not decorated)

projection_fields(OrderSummary)   # ["id", "status", "total"]
```

| Function | Return Type | Description |
|---|---|---|
| `is_projection(cls)` | `bool` | Check if a type is marked with `@projection` |
| `projection_fields(cls)` | `list[str]` | Get the field names declared on a projection type |

### Mapper.register_projection() and Mapper.project()

`Mapper` supports projections alongside its standard `map()` method. Use `register_projection()` to optionally add computed-field transforms, then `project()` to map entities:

```python
from pyfly.data import Mapper
from pyfly.data.projection import projection


@projection
class OrderSummary(Protocol):
    id: str
    status: str
    line_total: float  # computed field


mapper = Mapper()
mapper.register_projection(Order, OrderSummary, transforms={
    "line_total": lambda order: order.quantity * order.unit_price,
})

summary = mapper.project(order, OrderSummary)
# summary.id, summary.status come from field-name matching
# summary.line_total is computed by the transform
```

**Key differences from `map()`:**

| Feature | `map()` | `project()` |
|---|---|---|
| Field mapping | Configurable via `add_mapping()` | Matches by name only |
| Transforms | Receive the *field value* | Receive the *entire source object* |
| Use case | DTO mapping between full types | Selecting a subset of fields |

**`register_projection()` parameters:**

| Parameter | Type | Description |
|---|---|---|
| `source_type` | `type[S]` | The source entity type |
| `projection_type` | `type[D]` | The projection Protocol type |
| `transforms` | `dict[str, Callable] \| None` | `{dest_field: fn(source)}` — callable receives the full source object |

If a projection field has no registered transform, `project()` falls back to standard field-name matching from the source object.

### Query Compiler Integration

When a repository method returns a projection type, the query compiler automatically selects only the columns declared by the projection. This reduces data transfer and improves performance:

```python
class OrderRepository(Repository[Order, str]):
    async def find_by_status(self, status: str) -> list[OrderSummary]: ...
```

The compiler calls `projection_fields(OrderSummary)` to determine which columns to SELECT, rather than fetching the full entity.

Source files:
- `src/pyfly/data/projection.py` — `@projection`, `is_projection`, `projection_fields`
- `src/pyfly/data/mapper.py` — `Mapper.register_projection()`, `Mapper.project()`

---

## Specification Port

The `Specification[T, Q]` ABC defines the composable query predicate contract that all data adapters implement. It enables building arbitrarily complex queries with `&` (AND), `|` (OR), and `~` (NOT) operators in an adapter-agnostic way.

```python
from pyfly.data import Specification  # ABC
```

**Type Parameters:**
- `T` — The entity type
- `Q` — The backend query representation (e.g., `sqlalchemy.Select`, `dict`)

**Abstract methods:**

| Method | Description |
|--------|-------------|
| `to_predicate(root, query)` | Apply this specification's predicate to a query |
| `__and__(other)` | Combine with AND |
| `__or__(other)` | Combine with OR |
| `__invert__()` | Negate (NOT) |

**Adapter implementations:**

| Adapter | Class | Query Type (`Q`) | Import |
|---------|-------|-------------------|--------|
| SQLAlchemy | `Specification[T]` | `sqlalchemy.Select` | `from pyfly.data.relational.sqlalchemy import Specification` |
| MongoDB | `MongoSpecification[T]` | `dict[str, Any]` | `from pyfly.data.document.mongodb import MongoSpecification` |

Source file: `src/pyfly/data/specification.py`

---

## BaseFilterUtils Port

The `BaseFilterUtils` ABC provides shared Query by Example logic. Subclasses supply adapter-specific factories (`_create_eq`, `_create_noop`) while inheriting the shared `by()`, `from_dict()`, and `from_example()` algorithms.

```python
from pyfly.data import BaseFilterUtils  # ABC
```

**Inherited methods (shared algorithm):**

| Method | Input | Behavior |
|--------|-------|----------|
| `by(**kwargs)` | Keyword arguments | All eq, ANDed together |
| `from_dict(filters)` | `dict[str, Any]` | All eq, ANDed; `None` values skipped |
| `from_example(example)` | Dataclass or object | Non-`None` fields become eq predicates |

**Abstract hooks (adapter-specific):**

| Method | Description |
|--------|-------------|
| `_create_eq(field, value)` | Create an equality specification for the backend |
| `_create_noop()` | Create a no-op specification that matches everything |

**Adapter implementations:**

| Adapter | Class | Import |
|---------|-------|--------|
| SQLAlchemy | `FilterUtils` | `from pyfly.data.relational.sqlalchemy import FilterUtils` |
| MongoDB | `MongoFilterUtils` | `from pyfly.data.document.mongodb import MongoFilterUtils` |

Source file: `src/pyfly/data/filter.py`

---

## BaseRepositoryPostProcessor Port

The `BaseRepositoryPostProcessor` ABC provides the shared iteration loop, stub detection, and derived-query prefix matching used by all adapter post-processors. Adapter-specific behaviour is supplied via abstract hook methods.

```python
from pyfly.data import BaseRepositoryPostProcessor, DERIVED_PREFIXES
```

**Shared behaviour:**
- `before_init(bean, bean_name)` — Returns the bean unchanged (default no-op)
- `after_init(bean, bean_name)` — Iterates class attributes, detects stubs, compiles derived queries
- `_is_stub(method)` — Bytecode analysis to detect `...` or `pass` stubs
- `DERIVED_PREFIXES` — `("find_by_", "count_by_", "exists_by_", "delete_by_")`

**Abstract hooks:**

| Method | Description |
|--------|-------------|
| `_get_repository_type()` | Return the base repository class this processor targets |
| `_compile_derived(parsed, entity, bean)` | Compile a parsed derived query into a callable |
| `_wrap_derived_method(compiled_fn)` | Wrap a compiled function for binding onto the bean |
| `_process_query_decorated(...)` | Handle decorator-based queries (default: no-op) |

**Adapter implementations:**

| Adapter | Class | Import |
|---------|-------|--------|
| SQLAlchemy | `RepositoryBeanPostProcessor` | `from pyfly.data.relational.sqlalchemy import RepositoryBeanPostProcessor` |
| MongoDB | `MongoRepositoryBeanPostProcessor` | `from pyfly.data.document.mongodb import MongoRepositoryBeanPostProcessor` |

Source file: `src/pyfly/data/post_processor.py`

---

## Extending PyFly Data

The PyFly Data architecture is designed to support additional database adapters by implementing the same patterns that the SQLAlchemy and MongoDB adapters use.

### How to Create a Custom Adapter

To add support for a new database backend (e.g., DynamoDB), you would:

1. **Create the adapter package:** `pyfly/data/document/dynamodb/`

2. **Implement a base entity/document class** (analogous to `BaseEntity` or `BaseDocument`):

```python
class BaseDynamoDocument:
    """Base document for DynamoDB items with audit fields."""
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None
```

3. **Implement the repository** (satisfying `RepositoryPort[T, ID]`):

```python
class DynamoRepository(Generic[T, ID]):
    """Generic CRUD repository for DynamoDB."""
    async def save(self, entity: T) -> T: ...
    async def find_by_id(self, id: ID) -> T | None: ...
    async def find_all(self, **filters: Any) -> list[T]: ...
    async def delete(self, id: ID) -> None: ...
    async def count(self) -> int: ...
    async def exists(self, id: ID) -> bool: ...
    async def find_paginated(self, page=1, size=20, pageable=None) -> Page[T]: ...
```

4. **Implement the query compiler** (satisfying `QueryMethodCompilerPort`):

```python
from pyfly.data.query_parser import ParsedQuery


class DynamoQueryMethodCompiler:
    """Compile ParsedQuery into DynamoDB query/scan operations."""

    def compile(self, parsed: ParsedQuery, entity: type[T]) -> Callable[..., Coroutine]:
        # Translate parsed predicates into DynamoDB expressions
        ...
```

5. **Implement the post-processor** (the `BeanPostProcessor` that wires derived query methods):

```python
class DynamoRepositoryBeanPostProcessor:
    def __init__(self) -> None:
        self._query_parser = QueryMethodParser()
        self._query_compiler = DynamoQueryMethodCompiler()

    def after_init(self, bean, bean_name):
        if not isinstance(bean, DynamoRepository):
            return bean
        # Parse and compile derived query methods, same pattern as MongoDB
        ...
```

6. **Add auto-detection** in `AutoConfiguration`:

```python
@staticmethod
def detect_dynamodb_provider() -> str:
    if AutoConfiguration.is_available("aiobotocore"):
        return "dynamodb"
    return "none"
```

### QueryMethodCompilerPort Contract

The key insight: the `QueryMethodParser` is fully shared — you never need to reimplement the parsing logic. The parser produces `ParsedQuery` objects that are backend-agnostic. Your adapter only needs to compile those parsed queries into the target database's query format.

This architecture means the naming convention for derived query methods (`find_by_status_and_role_order_by_name_desc`) is consistent across all PyFly data adapters. Once you learn the convention, it works the same way regardless of backend.

### BeanPostProcessor Pattern

Each adapter follows the same wiring pattern:

1. A `BeanPostProcessor` scans repository beans after initialization.
2. It detects stub methods (method bodies that are just `...` or `pass`).
3. For derived query methods (`find_by_*`, `count_by_*`, etc.): parses the method name via `QueryMethodParser`, compiles it via the adapter's compiler, and replaces the stub.
4. For `@query`-decorated methods: compiles the query string into an executable callable.

Source files:
- `src/pyfly/data/ports/compiler.py` — `QueryMethodCompilerPort` protocol
- `src/pyfly/data/query_parser.py` — `QueryMethodParser` (shared)
- `src/pyfly/data/relational/sqlalchemy/query_compiler.py` — SQLAlchemy implementation
- `src/pyfly/data/document/mongodb/query_compiler.py` — MongoDB implementation

---

## Choosing a Query Mechanism

PyFly offers four ways to express a read, plus pagination that layers on top of any of them.
Reach for the simplest one that fits, and escalate only when it can't express the query:

| Mechanism | Use it when | Avoid it when | Example |
|-----------|-------------|---------------|---------|
| **Derived query methods** (`find_by_…`) | The predicate is a fixed combination of fields and the [supported operators](#operators), joined by AND/OR, optionally ordered. Type-safe, no SQL. **Start here.** | The query needs joins, aggregation, `OR` across more than a couple of fields, or an operator PyFly doesn't derive (see limitations). | `find_by_status_and_total_greater_than(...)` |
| **`@query`** (JPQL-like or `native=True`) | The query is too complex to name — joins, aggregates, hand-tuned SQL, or a MongoDB filter document. | A derived method would read clearly — don't write SQL for `find_by_id`. | `@query("SELECT o FROM Order o WHERE o.total > :min")` |
| **Specification** | The predicate is **dynamic** — built at runtime from optional filters and composed with `&` / `\|` / `~`. | The predicate is fixed (use a derived method) or static (use `@query`). | `active & (admin \| owner)` |
| **Query-by-Example** (`FilterUtils.from_example` / `from_dict`) | You have a populated example object or a dict of equality filters (e.g. straight from API query params). | You need ranges, `OR`, or anything beyond field-equality `AND`. | `FilterUtils.from_dict({"status": "ACTIVE"})` |

Then wrap any of the above with **`Pageable` + `Sort`** for paginated, ordered results
(`find_paginated`, `find_all_by_spec_paged`). Use `Pageable.unpaged()` to fetch everything.

## Spring Data Parity & Current Limitations

PyFly's repositories are Spring-Data-equivalent for the everyday feature set — across **both**
backends: CRUD + batch ops, `Pageable`/`Sort`/`Page`, derived queries, `@query`, Specifications,
Query-by-Example, projections, repository auto-implementation, and generic-type extraction are all
implemented and behavior-tested on relational **and** document.

Some capabilities are **backend-specific** today:

| Capability | Relational (SQLAlchemy) | Document (MongoDB) |
|------------|:-----------------------:|:------------------:|
| CRUD + batch, Pageable/Sort/Page, derived queries, `@query`, Specifications, QBE | ✅ | ✅ |
| Projections | ✅ | ✅ (closed/field-subset) |
| Soft delete (`SoftDeleteRepository`) | ✅ | ❌ not yet |
| Optimistic locking (`VersionedMixin` / `@Version`) | ✅ | ❌ not yet |
| Auditing auto-population | ✅ `created/updated_at` **and** `created/updated_by` | ⚠️ timestamps at insert only |
| `@transactional` — one annotation, both backends (`pyfly.data`) | ✅ propagation / isolation / read-only / `rollback_for` | ✅ commit/abort + `rollback_for` (replica set; propagation/isolation are relational-only) |

**Not yet implemented on either backend** (so you don't reach for them): `@Modifying`-style
declarative bulk `UPDATE`/`DELETE`; `Slice` (count-less paging) and streaming/reactive result
types; DTO / open (SpEL) / dynamic / association-traversing projections; the derived-query keywords
`StartingWith` / `EndingWith` / `IgnoreCase` / `True` / `False` / `Distinct` / `Top<N>` /
`After` / `Before` (use `_like`/`_containing`/`@query` instead); `ExampleMatcher` string-match
modes; named queries and `Pageable`/SpEL injection into `@query`. For these, fall back to `@query`
(or `native=True`) — it covers every case the derived parser doesn't.

---

## Available Adapters

| Adapter | Package | Backend | Guide |
|---------|---------|---------|-------|
| **SQLAlchemy** | `pyfly.data.relational.sqlalchemy` | PostgreSQL, MySQL, SQLite | [Data Relational Guide](data-relational.md) · [Adapter Reference](../adapters/sqlalchemy.md) |
| **MongoDB** | `pyfly.data.document.mongodb` | MongoDB (Beanie ODM) | [Data Document Guide](data-document.md) · [Adapter Reference](../adapters/mongodb.md) |

Both adapters can coexist in the same project. The CLI supports selecting both `data-relational` (SQL) and `data-document` features together.

---

## See Also

- [Data Relational Guide](data-relational.md) — SQLAlchemy adapter: entities, repositories, specifications, custom queries, transactions
- [Data Document Guide](data-document.md) — MongoDB adapter: documents, MongoRepository, Beanie ODM, transactions
- [SQLAlchemy Adapter Reference](../adapters/sqlalchemy.md) — Setup, configuration, adapter-specific features
- [MongoDB Adapter Reference](../adapters/mongodb.md) — Setup, configuration, adapter-specific features
- [Architecture Overview](../architecture.md) — Framework-wide hexagonal design
