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
"""Dynamic query building utilities for MongoDB.

Provides :class:`MongoFilterOperator` for individual field predicates and
:class:`MongoFilterUtils` for auto-generating :class:`MongoSpecification`
objects from partial entities, dicts, or keyword arguments.

Field names are the document's Python fields: ``id`` is matched as ``_id`` (an id is converted to the
document's id type) and an aliased field under its alias. A name that is not a field of the document raises
:class:`~pyfly.data.property_resolver.InvalidPropertyError` when the specification is applied (a dotted path
into an embedded document keeps what follows its first segment as written). The operators mean what they mean
on SQL (:mod:`~pyfly.data.document.mongodb.criteria`): ``like`` is anchored and case-sensitive, ``contains``
matches its argument as it is, and ``neq`` (like ``~eq``) is false for a null or missing field.

Example::

    # From keyword arguments (eq by default, ANDed together)
    spec = MongoFilterUtils.by(name="Alice", active=True)
    results = await repo.find_all_by_spec(spec)

    # Using operators directly for richer predicates
    spec = MongoFilterOperator.gte("age", 18) & MongoFilterOperator.lt("age", 65)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyfly.data.document.mongodb import criteria
from pyfly.data.document.mongodb.properties import ID_FIELD, InvalidIdError, coerce_id, field_path
from pyfly.data.document.mongodb.specification import MongoSpecification
from pyfly.data.filter import BaseFilterUtils


def _where(field: str, condition: Callable[[str, type], Any]) -> MongoSpecification[Any]:
    """A specification on *field*: *condition* receives its stored name and the document class."""

    def predicate(root: Any, query: dict[str, Any]) -> dict[str, Any]:
        path = field_path(root, field, usage="filter")
        return {path: condition(path, root)}

    return MongoSpecification(predicate)


def _id_values(path: str, root: Any, values: list[Any]) -> list[Any]:
    """*values* converted to the document's id type when *path* is ``_id`` (those it cannot hold left out)."""
    if path != ID_FIELD or not isinstance(root, type):
        return values
    converted = []
    for value in values:
        try:
            converted.append(coerce_id(root, value))
        except InvalidIdError:
            continue
    return converted


def _id_value(path: str, root: Any, value: Any) -> tuple[bool, Any]:
    """``(valid, value)``: *value* converted to the document's id type when *path* is ``_id``."""
    converted = _id_values(path, root, [value])
    return (True, converted[0]) if converted else (False, None)


class MongoFilterOperator:
    """MongoDB filter operators producing MongoSpecification instances.

    Each static method returns a :class:`MongoSpecification` that applies a
    single field-level predicate. Specifications can then be combined
    with ``&`` (AND), ``|`` (OR), and ``~`` (NOT, with SQL's semantics for null fields).
    """

    @staticmethod
    def eq(field: str, value: Any) -> MongoSpecification[Any]:
        """Equal to (``None``: the field is null or missing)."""

        def condition(path: str, root: Any) -> Any:
            valid, converted = _id_value(path, root, value)
            return criteria.equals(converted) if valid else {"$in": []}

        return _where(field, condition)

    @staticmethod
    def neq(field: str, value: Any) -> MongoSpecification[Any]:
        """Not equal to, as on SQL: false for a null or missing field (``None``: the field is not null)."""

        def condition(path: str, root: Any) -> Any:
            valid, converted = _id_value(path, root, value)
            return criteria.not_equals(converted) if valid else {"$exists": True}

        return _where(field, condition)

    @staticmethod
    def gt(field: str, value: Any) -> MongoSpecification[Any]:
        """Greater than."""
        return _where(field, lambda path, root: {"$gt": value})

    @staticmethod
    def gte(field: str, value: Any) -> MongoSpecification[Any]:
        """Greater than or equal."""
        return _where(field, lambda path, root: {"$gte": value})

    @staticmethod
    def lt(field: str, value: Any) -> MongoSpecification[Any]:
        """Less than."""
        return _where(field, lambda path, root: {"$lt": value})

    @staticmethod
    def lte(field: str, value: Any) -> MongoSpecification[Any]:
        """Less than or equal."""
        return _where(field, lambda path, root: {"$lte": value})

    @staticmethod
    def like(field: str, pattern: str, *, ignore_case: bool = False) -> MongoSpecification[Any]:
        """SQL ``LIKE``: *pattern* is anchored and case-sensitive (unless *ignore_case*); ``%`` matches any run
        of characters, ``_`` one character, every other character itself."""
        return _where(field, lambda path, root: criteria.like(pattern, ignore_case=ignore_case))

    @staticmethod
    def contains(field: str, value: str, *, ignore_case: bool = False) -> MongoSpecification[Any]:
        """String contains *value* as it is (case-sensitive unless *ignore_case*)."""
        return _where(
            field,
            lambda path, root: criteria.literal(value, anchor_start=False, anchor_end=False, ignore_case=ignore_case),
        )

    @staticmethod
    def in_list(field: str, values: list[Any]) -> MongoSpecification[Any]:
        """Value is in list (an empty list matches nothing)."""
        return _where(field, lambda path, root: {"$in": _id_values(path, root, criteria.listed(values))})

    @staticmethod
    def is_null(field: str) -> MongoSpecification[Any]:
        """Value is null or missing."""
        return _where(field, lambda path, root: None)

    @staticmethod
    def is_not_null(field: str) -> MongoSpecification[Any]:
        """Value is present and not null."""
        return _where(field, lambda path, root: criteria.is_not_null())

    @staticmethod
    def between(field: str, low: Any, high: Any) -> MongoSpecification[Any]:
        """Value is between *low* and *high* (inclusive)."""
        return _where(field, lambda path, root: {"$gte": low, "$lte": high})


class MongoFilterUtils(BaseFilterUtils):
    """Generate MongoSpecifications dynamically from entities, dicts, or kwargs.

    Usage::

        # From keyword arguments (eq by default)
        spec = MongoFilterUtils.by(name="Alice", active=True)

        # From a dict
        spec = MongoFilterUtils.from_dict({"name": "Alice", "active": True})

        # From a partial entity (non-None fields become eq filters)
        spec = MongoFilterUtils.from_example(UserFilter(role="admin"))
    """

    @staticmethod
    def _create_eq(field: str, value: Any) -> MongoSpecification[Any]:
        return MongoFilterOperator.eq(field, value)

    @staticmethod
    def _create_noop() -> MongoSpecification[Any]:
        return MongoSpecification(lambda root, q: {})

    @classmethod
    def _example_values(cls, example: Any) -> dict[str, Any]:
        """A pydantic example (a document or a DTO) by its fields; any other example as the base reads it."""
        fields = getattr(type(example), "model_fields", None)
        if isinstance(fields, dict):
            return {name: getattr(example, name) for name in fields}
        return super()._example_values(example)
