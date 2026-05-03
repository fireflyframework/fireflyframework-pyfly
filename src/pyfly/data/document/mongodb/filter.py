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

Example::

    # From keyword arguments (eq by default, ANDed together)
    spec = MongoFilterUtils.by(name="Alice", active=True)
    results = await repo.find_all_by_spec(spec)

    # Using operators directly for richer predicates
    spec = MongoFilterOperator.gte("age", 18) & MongoFilterOperator.lt("age", 65)
"""

from __future__ import annotations

import re
from typing import Any

from pyfly.data.document.mongodb.specification import MongoSpecification
from pyfly.data.filter import BaseFilterUtils


class MongoFilterOperator:
    """MongoDB filter operators producing MongoSpecification instances.

    Each static method returns a :class:`MongoSpecification` that applies a
    single field-level predicate. Specifications can then be combined
    with ``&`` (AND), ``|`` (OR), and ``~`` (NOT).
    """

    @staticmethod
    def eq(field: str, value: Any) -> MongoSpecification[Any]:
        """Equal to."""
        return MongoSpecification(lambda root, q, _f=field, _v=value: {_f: _v})  # type: ignore[misc]

    @staticmethod
    def neq(field: str, value: Any) -> MongoSpecification[Any]:
        """Not equal to."""
        return MongoSpecification(lambda root, q, _f=field, _v=value: {_f: {"$ne": _v}})  # type: ignore[misc]

    @staticmethod
    def gt(field: str, value: Any) -> MongoSpecification[Any]:
        """Greater than."""
        return MongoSpecification(lambda root, q, _f=field, _v=value: {_f: {"$gt": _v}})  # type: ignore[misc]

    @staticmethod
    def gte(field: str, value: Any) -> MongoSpecification[Any]:
        """Greater than or equal."""
        return MongoSpecification(lambda root, q, _f=field, _v=value: {_f: {"$gte": _v}})  # type: ignore[misc]

    @staticmethod
    def lt(field: str, value: Any) -> MongoSpecification[Any]:
        """Less than."""
        return MongoSpecification(lambda root, q, _f=field, _v=value: {_f: {"$lt": _v}})  # type: ignore[misc]

    @staticmethod
    def lte(field: str, value: Any) -> MongoSpecification[Any]:
        """Less than or equal."""
        return MongoSpecification(lambda root, q, _f=field, _v=value: {_f: {"$lte": _v}})  # type: ignore[misc]

    @staticmethod
    def like(field: str, pattern: str) -> MongoSpecification[Any]:
        """Regex pattern match (SQL LIKE equivalent).

        Converts SQL LIKE patterns (``%`` and ``_``) to regex equivalents.
        """
        # Replace unescaped LIKE wildcards with regex equivalents,
        # then escape the rest for safe regex usage.
        parts = []
        for char in pattern:
            if char == "%":
                parts.append(".*")
            elif char == "_":
                parts.append(".")
            else:
                parts.append(re.escape(char))
        regex = "^" + "".join(parts) + "$"
        return MongoSpecification(lambda root, q, _f=field, _r=regex: {_f: {"$regex": _r, "$options": "i"}})  # type: ignore[misc]

    @staticmethod
    def contains(field: str, value: str) -> MongoSpecification[Any]:
        """String contains (case-sensitive)."""
        escaped = re.escape(value)
        return MongoSpecification(lambda root, q, _f=field, _v=escaped: {_f: {"$regex": _v}})  # type: ignore[misc]

    @staticmethod
    def in_list(field: str, values: list[Any]) -> MongoSpecification[Any]:
        """Value is in list."""
        return MongoSpecification(lambda root, q, _f=field, _v=values: {_f: {"$in": _v}})  # type: ignore[misc]

    @staticmethod
    def is_null(field: str) -> MongoSpecification[Any]:
        """Value is null."""
        return MongoSpecification(lambda root, q, _f=field: {_f: None})  # type: ignore[misc]

    @staticmethod
    def is_not_null(field: str) -> MongoSpecification[Any]:
        """Value is not null."""
        return MongoSpecification(lambda root, q, _f=field: {_f: {"$ne": None}})  # type: ignore[misc]

    @staticmethod
    def between(field: str, low: Any, high: Any) -> MongoSpecification[Any]:
        """Value is between *low* and *high* (inclusive)."""
        return MongoSpecification(lambda root, q, _f=field, _lo=low, _hi=high: {_f: {"$gte": _lo, "$lte": _hi}})  # type: ignore[misc]


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
