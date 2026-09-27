# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Validation of the property names a caller sorts, filters or queries by example on.

Sort orders, ``find_all(**filters)`` keys, ``FilterUtils`` and query-by-example names often come straight
from a request. :class:`PropertyResolver` accepts only the entity's own mapped properties: a relationship,
a Python ``@property``, a private or dunder name, a typo, or a Mongo operator key (anything with ``$``) raises
:class:`InvalidPropertyError`, an :class:`~pyfly.kernel.exceptions.InvalidRequestException` the web layer
answers with 400, instead of a 500 from deep inside the query layer or an operator forwarded to the server.
An allow-list narrows the properties further, so a hidden column (``password_hash``) is neither sortable
nor filterable.

The resolver also maps a property to the backend's own name: a SQLAlchemy entity's mapped attributes map to
themselves, a pydantic model's fields to their alias. Other kinds of entity are described by an introspector
registered with :func:`register_property_introspector` (the document backend maps ``id`` to ``_id`` that way).
This module imports no backend at module scope.
"""

from __future__ import annotations

import dataclasses
import threading
import weakref
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any

from pyfly.data.pageable import Sort
from pyfly.kernel.exceptions import InvalidRequestException

PropertyIntrospector = Callable[[type], Mapping[str, str] | None]
"""Describes an entity class: its property names mapped to the backend's names, or ``None`` when it does
not know that kind of class."""

_INTROSPECTORS: list[PropertyIntrospector] = []
_CACHE: weakref.WeakKeyDictionary[type, Mapping[str, str]] = weakref.WeakKeyDictionary()
_LOCK = threading.Lock()

_LISTED = 20
"""How many valid names an error message lists."""


class InvalidPropertyError(InvalidRequestException, ValueError):
    """A property name the entity does not have, or may not be used for this purpose (sort, filter...)."""

    def __init__(self, message: str, *, entity: str, property: str, usage: str) -> None:
        super().__init__(
            message,
            code="INVALID_PROPERTY",
            context={"entity": entity, "property": property, "usage": usage},
        )
        self.entity = entity
        self.property = property
        self.usage = usage


def register_property_introspector(introspector: PropertyIntrospector) -> None:
    """Register *introspector*; it is asked before the built-in ones (SQLAlchemy, pydantic)."""
    with _LOCK:
        _INTROSPECTORS.insert(0, introspector)
        _CACHE.clear()


def unregister_property_introspector(introspector: PropertyIntrospector) -> None:
    """Remove an introspector :func:`register_property_introspector` added (a missing one is ignored)."""
    with _LOCK:
        if introspector in _INTROSPECTORS:
            _INTROSPECTORS.remove(introspector)
        _CACHE.clear()


def _sqlalchemy_properties(entity: type) -> Mapping[str, str] | None:
    try:
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.orm import Mapper
    except ImportError:  # pragma: no cover — the relational extra is not installed
        return None
    mapper: Any = sa_inspect(entity, raiseerr=False)
    if not isinstance(mapper, Mapper):
        return None
    return {attribute.key: attribute.key for attribute in mapper.column_attrs}


def _pydantic_properties(entity: type) -> Mapping[str, str] | None:
    fields = getattr(entity, "model_fields", None)
    if not isinstance(fields, Mapping):
        return None
    return {name: (getattr(info, "alias", None) or name) for name, info in fields.items()}


def _introspect(entity: type) -> Mapping[str, str]:
    cached = _CACHE.get(entity)
    if cached is not None:
        return cached
    with _LOCK:
        introspectors = [*_INTROSPECTORS, _sqlalchemy_properties, _pydantic_properties]
    for introspector in introspectors:
        found = introspector(entity)
        if found is not None:
            properties: Mapping[str, str] = MappingProxyType(dict(found))
            with _LOCK:
                _CACHE[entity] = properties
            return properties
    raise TypeError(
        f"Cannot tell the properties of {entity.__name__}: it is neither a SQLAlchemy mapped class nor a pydantic "
        "model, and no registered property introspector describes it"
    )


def _listing(names: Iterable[str]) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:_LISTED])
    return shown + (f", ... ({len(ordered) - _LISTED} more)" if len(ordered) > _LISTED else "")


class PropertyResolver:
    """Validates property names against an entity and maps them to the backend's names.

    *properties* maps each usable property name to its backend name; :meth:`for_entity` builds it from
    the entity class. *allowed*, when given, is the only names that resolve (each must be a property of the
    entity, or ``ValueError`` is raised at once: an allow-list with a typo would hide a column silently).
    """

    __slots__ = ("_entity", "_properties")

    def __init__(self, entity: type, properties: Mapping[str, str], *, allowed: Iterable[str] | None = None) -> None:
        self._entity = entity
        if allowed is not None:
            names = tuple(allowed)
            unknown = [name for name in names if name not in properties]
            if unknown:
                raise ValueError(
                    f"The allow-list of {entity.__name__} names {', '.join(map(repr, unknown))}, which "
                    f"{'is not a property' if len(unknown) == 1 else 'are not properties'} of it; its properties: "
                    f"{_listing(properties)}"
                )
            properties = {name: properties[name] for name in names}
        self._properties: Mapping[str, str] = MappingProxyType(dict(properties))

    @classmethod
    def for_entity(cls, entity: type, *, allowed: Iterable[str] | None = None) -> PropertyResolver:
        """The resolver of *entity* (a SQLAlchemy mapped class, a pydantic model, or a class a registered
        introspector describes), narrowed to *allowed* when given."""
        return cls(entity, _introspect(entity), allowed=allowed)

    @property
    def entity(self) -> type:
        """The entity class the names belong to."""
        return self._entity

    @property
    def properties(self) -> Mapping[str, str]:
        """The names that resolve, mapped to the backend's names."""
        return self._properties

    def resolve(self, name: str, *, usage: str = "property") -> str:
        """The backend name of property *name*; raises :class:`InvalidPropertyError` naming *usage* (``sort``,
        ``filter``, ``example``...) when *name* is not one of :attr:`properties`."""
        entity = self._entity.__name__
        if not isinstance(name, str) or "$" in name:
            raise InvalidPropertyError(
                f"{usage.capitalize()} property {name!r} of {entity} is not allowed: property names may not contain "
                "'$' (a query operator)",
                entity=entity,
                property=str(name),
                usage=usage,
            )
        resolved = self._properties.get(name)
        if resolved is None:
            raise InvalidPropertyError(
                f"{entity} has no {usage} property {name!r}; {usage} properties: {_listing(self._properties)}",
                entity=entity,
                property=name,
                usage=usage,
            )
        return resolved

    def resolve_all(self, names: Iterable[str], *, usage: str = "property") -> list[str]:
        """The backend names of *names*, in order (the first invalid one raises)."""
        return [self.resolve(name, usage=usage) for name in names]

    def resolve_sort(self, sort: Sort) -> Sort:
        """*sort* with every order's property validated and mapped to its backend name."""
        return Sort(
            orders=tuple(
                dataclasses.replace(order, property=self.resolve(order.property, usage="sort")) for order in sort.orders
            )
        )

    def __repr__(self) -> str:
        return f"PropertyResolver({self._entity.__name__}, {sorted(self._properties)})"
