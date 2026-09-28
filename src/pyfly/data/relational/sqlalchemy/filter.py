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
"""Dynamic query building utilities for generating Specifications.

Provides :class:`FilterOperator` for individual column predicates and
:class:`FilterUtils` for auto-generating :class:`Specification` objects
from partial entities, dicts, or keyword arguments.  This is PyFly's
take on Spring Data's *Query by Example* pattern, but more Pythonic.

Example::

    # From keyword arguments (eq by default, ANDed together)
    spec = FilterUtils.by(name="Alice", active=True)
    results = await repo.find_all_by_spec(spec)

    # From a dict (None values are skipped)
    spec = FilterUtils.from_dict({"role": "admin", "name": None})

    # From a partial entity / dataclass
    spec = FilterUtils.from_example(UserFilter(role="admin"))
    spec = FilterUtils.from_example(User(role="admin"))

    # Using operators directly for richer predicates
    spec = FilterOperator.gte("age", 18) & FilterOperator.lt("age", 65)

**Names are validated** when a specification is applied, against the entity it is applied to
(:class:`~pyfly.data.property_resolver.PropertyResolver`): its columns, synonyms and hybrids, its
relationships to one entity (compared with an instance of that entity or ``None``), and its composites (compared
with a value of their class or ``None``, which means every column null, through ``eq``, ``neq``, ``is_null`` and
``is_not_null``; ``neq`` is the negation of ``eq``, as a derived query's ``_not``). Anything else (a typo, a
Python ``@property``, a private or dunder name, a name with ``$``) raises
:class:`~pyfly.data.property_resolver.InvalidPropertyError`, which the web layer answers with 400, so filters
straight from a request never reach ``getattr``. A repository's ``__filterable__`` allow-list narrows
``find_all(**filters)``; filter a hidden column out of a request's dictionary before it becomes a specification.

``contains`` matches its value as it is (``%`` and ``_`` are plain characters); ``like`` takes a pattern.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import Select, not_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import InstanceState, Mapper

from pyfly.data.filter import BaseFilterUtils
from pyfly.data.property_resolver import InvalidPropertyError, PropertyResolver
from pyfly.data.relational.sqlalchemy.specification import Specification

_RESOLVERS: weakref.WeakKeyDictionary[type, PropertyResolver] = weakref.WeakKeyDictionary()
_RESOLVERS_LOCK = threading.Lock()


def filter_properties(entity: type) -> PropertyResolver:
    """The names a filter of *entity* may use: its properties, its relationships to one entity and its
    composites."""
    resolver = _RESOLVERS.get(entity)
    if resolver is None:
        properties = dict(PropertyResolver.for_entity(entity).properties)
        mapper: Mapper[Any] = sa_inspect(entity)
        properties.update(
            (relationship.key, relationship.key)
            for relationship in mapper.relationships
            if not relationship.uselist and not relationship.key.startswith("_")
        )
        properties.update((prop.key, prop.key) for prop in mapper.composites if not prop.key.startswith("_"))
        resolver = PropertyResolver(entity, properties)
        with _RESOLVERS_LOCK:
            _RESOLVERS[entity] = resolver
    return resolver


def _where(
    field: str, condition: Callable[[Any], Any], composite: Callable[[Any], Any] | None = None
) -> Specification[Any]:
    """A specification adding ``condition(attribute)`` for the entity's attribute *field* (``root`` is the entity
    class or an alias of it, the name validated), or ``composite(attribute)`` when the attribute is a composite:
    an operator without one does not apply to a composite."""

    def predicate(root: Any, query: Select[Any]) -> Select[Any]:
        mapper: Mapper[Any] = cast(Any, sa_inspect(root)).mapper
        key = filter_properties(mapper.class_).resolve(field, usage="filter")
        attribute = getattr(root, key)
        if key not in mapper.composites:
            return query.where(condition(attribute))
        if composite is None:
            raise InvalidPropertyError(
                f"{mapper.class_.__name__}.{field} is a composite: it compares with a value of its class or None "
                "(eq, neq, is_null, is_not_null)",
                entity=mapper.class_.__name__,
                property=field,
                usage="filter",
            )
        return query.where(composite(attribute))

    return Specification(predicate)


def _composite_equals(attribute: Any, value: Any) -> Any:
    """A composite equal to *value*, column by column (every column null for ``None``)."""
    return attribute == value


class FilterOperator:
    """Filter operators for building dynamic specifications.

    Each static method returns a :class:`Specification` that applies a
    single column-level predicate.  Specifications can then be combined
    with ``&`` (AND), ``|`` (OR), and ``~`` (NOT). Field names are validated when the specification is applied
    (module documentation).
    """

    @staticmethod
    def eq(field: str, value: Any) -> Specification[Any]:
        """Equal to (a relationship to one entity compares with an instance, a composite with a value of its
        class; both with ``None`` too)."""
        return _where(field, lambda column: column == value, lambda held: _composite_equals(held, value))

    @staticmethod
    def neq(field: str, value: Any) -> Specification[Any]:
        """Not equal to (a composite: not equal to *value* as a whole, the negation of :meth:`eq`)."""
        return _where(field, lambda column: column != value, lambda held: not_(_composite_equals(held, value)))

    @staticmethod
    def gt(field: str, value: Any) -> Specification[Any]:
        """Greater than."""
        return _where(field, lambda column: column > value)

    @staticmethod
    def gte(field: str, value: Any) -> Specification[Any]:
        """Greater than or equal."""
        return _where(field, lambda column: column >= value)

    @staticmethod
    def lt(field: str, value: Any) -> Specification[Any]:
        """Less than."""
        return _where(field, lambda column: column < value)

    @staticmethod
    def lte(field: str, value: Any) -> Specification[Any]:
        """Less than or equal."""
        return _where(field, lambda column: column <= value)

    @staticmethod
    def like(field: str, pattern: str) -> Specification[Any]:
        """SQL LIKE pattern match (``%`` and ``_`` in *pattern* are wildcards)."""
        return _where(field, lambda column: column.like(pattern))

    @staticmethod
    def contains(field: str, value: str) -> Specification[Any]:
        """String contains *value* as it is: its ``%`` and ``_`` are escaped (``LIKE '%value%' ESCAPE '/'``)."""
        return _where(field, lambda column: column.contains(str(value), autoescape=True))

    @staticmethod
    def in_list(field: str, values: list[Any]) -> Specification[Any]:
        """Value is in list."""
        return _where(field, lambda column: column.in_(values))

    @staticmethod
    def is_null(field: str) -> Specification[Any]:
        """Value is NULL (a composite: every column of it)."""
        return _where(field, lambda column: column.is_(None), lambda held: _composite_equals(held, None))

    @staticmethod
    def is_not_null(field: str) -> Specification[Any]:
        """Value is NOT NULL (a composite: not every column of it)."""
        return _where(field, lambda column: column.isnot(None), lambda held: not_(_composite_equals(held, None)))

    @staticmethod
    def between(field: str, low: Any, high: Any) -> Specification[Any]:
        """Value is between *low* and *high* (inclusive)."""
        return _where(field, lambda column: column.between(low, high))


class FilterUtils(BaseFilterUtils):
    """Generate Specifications dynamically from entities, dicts, or kwargs.

    This is PyFly's equivalent of Spring Data's *Query by Example*.

    Usage::

        # From keyword arguments (eq by default)
        spec = FilterUtils.by(name="Alice", active=True)

        # From a dict
        spec = FilterUtils.from_dict({"name": "Alice", "active": True})

        # From a partial entity (non-None fields become eq filters)
        spec = FilterUtils.from_example(User(name="Alice"))

    An entity probe contributes the mapped column attributes it holds (loaded, or set on a new instance) that
    are not ``None``, by attribute name: a loaded entity therefore matches on its key and every loaded column,
    as Spring's ``Example.of(entity)`` does. Relationships and attributes that are not loaded are left out.
    """

    @staticmethod
    def _create_eq(field: str, value: Any) -> Specification[Any]:
        return FilterOperator.eq(field, value)

    @staticmethod
    def _create_noop() -> Specification[Any]:
        return Specification(lambda root, q: q)

    @classmethod
    def _example_values(cls, example: Any) -> dict[str, Any]:
        state = sa_inspect(example, raiseerr=False)
        if not isinstance(state, InstanceState):
            return super()._example_values(example)
        held = state.dict
        return {
            attribute.key: held[attribute.key]
            for attribute in state.mapper.column_attrs
            if attribute.key in held and not attribute.key.startswith("_")
        }
