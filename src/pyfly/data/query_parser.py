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
"""Derived query method parser for Spring Data-style repositories.

Parses method names like ``find_by_status_and_role_order_by_name_desc`` into
structured query descriptions. Framework-agnostic — contains no backend
imports. Each data adapter provides its own compiler via
:class:`~pyfly.data.ports.compiler.QueryMethodCompilerPort`.

Grammar
-------
**Prefixes:** ``find_by``, ``count_by``, ``exists_by``, ``delete_by``

**Connectors:** ``_and_``, ``_or_``

**Operators (suffix on field name):**
    - *(none)* = equals (default)
    - ``_greater_than`` = ``>``
    - ``_less_than`` = ``<``
    - ``_greater_than_equal`` = ``>=``
    - ``_less_than_equal`` = ``<=``
    - ``_between`` = BETWEEN (takes 2 args)
    - ``_like`` = LIKE
    - ``_containing`` = CONTAINS (wraps in ``%``)
    - ``_in`` = IN (takes list arg)
    - ``_not`` = ``!=``
    - ``_is_null`` = IS NULL (no arg)
    - ``_is_not_null`` = IS NOT NULL (no arg)

**Ordering suffix:** ``_order_by_{field}_{asc|desc}`` (can chain multiple)

Example::

    parser = QueryMethodParser()
    parsed = parser.parse("find_by_status_and_role_order_by_name_desc")
    # -> ParsedQuery with predicates and order clauses
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Operator suffixes ordered longest-first to prevent partial matches.
# E.g., ``_greater_than_equal`` must be checked before ``_greater_than``.
OPERATORS: dict[str, str] = {
    "_greater_than_equal": "gte",
    "_less_than_equal": "lte",
    "_greater_than": "gt",
    "_less_than": "lt",
    "_is_not_null": "is_not_null",
    "_is_null": "is_null",
    "_containing": "containing",
    "_between": "between",
    "_not": "not",
    "_like": "like",
    "_in": "in",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FieldPredicate:
    """A single field predicate parsed from a method name."""

    field_name: str
    operator: str = "eq"  # default is equals


@dataclass
class OrderClause:
    """A single order-by clause."""

    field_name: str
    direction: str = "asc"


@dataclass
class ParsedQuery:
    """Result of parsing a query method name."""

    prefix: str  # find_by, count_by, exists_by, delete_by
    predicates: list[FieldPredicate] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)  # "and" or "or" between predicates
    order_clauses: list[OrderClause] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class QueryMethodParser:
    """Parse method names into structured query descriptions.

    Examples::

        parse("find_by_email")                         -> find where email = ?
        parse("find_by_status_and_role")               -> find where status = ? AND role = ?
        parse("find_by_age_greater_than")              -> find where age > ?
        parse("find_by_name_order_by_created_at_desc") -> find where name = ? ORDER BY created_at DESC
        parse("count_by_active")                       -> count where active = ?
        parse("exists_by_email")                       -> exists where email = ?
    """

    PREFIXES = ("find_by_", "count_by_", "exists_by_", "delete_by_")

    def parse(self, method_name: str) -> ParsedQuery:
        """Parse a method name into a :class:`ParsedQuery`."""
        # 1. Extract prefix
        prefix: str | None = None
        body = method_name
        for p in self.PREFIXES:
            if method_name.startswith(p):
                prefix = p.rstrip("_")  # e.g. "find_by"
                body = method_name[len(p) :]
                break
        if prefix is None:
            raise ValueError(f"Method name must start with one of {self.PREFIXES}: {method_name}")

        # 2. Split off order_by suffix
        order_clauses: list[OrderClause] = []
        order_match = re.search(r"_order_by_(.+)$", body)
        if order_match:
            order_body = order_match.group(1)
            body = body[: order_match.start()]
            order_clauses = self._parse_order(order_body)

        # 3. Split body by _and_ / _or_ connectors and parse each predicate
        predicates, connectors = self._parse_predicates(body)

        return ParsedQuery(
            prefix=prefix,
            predicates=predicates,
            connectors=connectors,
            order_clauses=order_clauses,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_order(order_body: str) -> list[OrderClause]:
        """Parse ``field_asc_field2_desc`` into a list of :class:`OrderClause`."""
        clauses: list[OrderClause] = []
        parts = order_body.split("_")
        i = 0
        while i < len(parts):
            field_parts: list[str] = []
            while i < len(parts) and parts[i] not in ("asc", "desc"):
                field_parts.append(parts[i])
                i += 1
            field_name = "_".join(field_parts)
            direction = "asc"
            if i < len(parts) and parts[i] in ("asc", "desc"):
                direction = parts[i]
                i += 1
            if field_name:
                clauses.append(OrderClause(field_name=field_name, direction=direction))
        return clauses

    @staticmethod
    def _parse_predicates(body: str) -> tuple[list[FieldPredicate], list[str]]:
        """Split the predicate body by ``_and_`` / ``_or_`` and parse each segment."""
        if not body:
            return [], []

        # Split by _and_ and _or_, keeping the connector tokens
        parts = re.split(r"(_and_|_or_)", body)
        predicates: list[FieldPredicate] = []
        connectors: list[str] = []

        for part in parts:
            if part == "_and_":
                connectors.append("and")
            elif part == "_or_":
                connectors.append("or")
            else:
                predicates.append(QueryMethodParser._parse_single_predicate(part))

        return predicates, connectors

    @staticmethod
    def _parse_single_predicate(segment: str) -> FieldPredicate:
        """Parse a single ``field[_operator]`` segment like ``age_greater_than``."""
        # Try operators from longest to shortest to avoid partial matches.
        for suffix, op in OPERATORS.items():
            if segment.endswith(suffix):
                field_name = segment[: -len(suffix)]
                return FieldPredicate(field_name=field_name, operator=op)
        # No operator suffix means equals.
        return FieldPredicate(field_name=segment, operator="eq")
