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
"""MongoDB filter criteria with the relational backend's semantics.

Derived queries (:mod:`~pyfly.data.document.mongodb.query_compiler`),
:class:`~pyfly.data.document.mongodb.filter.MongoFilterOperator` and
:class:`~pyfly.data.document.mongodb.specification.MongoSpecification` build their filters here, so a query
means the same thing on MongoDB as on a SQL database:

- **Pattern matching (C036)** follows SQL ``LIKE``: a ``like`` pattern is anchored at both ends and
  case-sensitive (``%`` is any run of characters, ``_`` one character, every other character is literal);
  ``containing``, ``starting_with`` and ``ending_with`` match their argument as it is. ``ignore_case`` makes a
  match case-insensitive. The regular expression starts with ``^`` whenever the pattern has a literal
  prefix, so MongoDB can use an index on the field; a case-insensitive or unanchored match cannot.
- **Negation (C110)** follows SQL's three-valued logic: ``!=``, ``NOT IN``, ``NOT LIKE`` and ``~spec`` are
  false, not true, for a document whose field is null or missing, as ``NULL <> 'x'`` is on SQL, where a
  plain ``$ne``/``$nin``/``$nor`` would match it. ``IS NULL`` and ``IS NOT NULL`` are never unknown.
  :func:`negate` computes the documents for which a filter is *false*: comparisons, ``$in``, regular
  expressions and ``$and``/``$or``/``$nor`` are negated exactly; an operator with no SQL counterpart
  (``$elemMatch``, ``$expr``...) falls back to MongoDB's own ``$nor``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

__all__ = [
    "equals",
    "ignoring_case",
    "in_values",
    "is_not_null",
    "like",
    "listed",
    "literal",
    "negate",
    "not_equals",
    "not_in_values",
]

_COMPARISONS = frozenset({"$gt", "$gte", "$lt", "$lte"})
_REGEX_OPTIONS = "$options"


def listed(value: Any) -> list[Any]:
    """The values of an ``IN`` argument: an iterable by its elements; a string, bytes, a mapping or any other
    value as a list of one (a single string never matches letter by letter)."""
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Collection):
        return [value]
    return list(value)


def _like_expression(pattern: str) -> str:
    """The regular expression of a SQL ``LIKE`` *pattern*, anchored (``%`` any run, ``_`` one character)."""
    parts: list[str] = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    expression = "^" + "".join(parts) + r"\z"
    # A trailing or leading "any run" needs no anchor: "^Al.*\z" is "^Al", and "^.*x" is "x".
    while expression.endswith(r".*\z"):
        expression = expression[: -len(r".*\z")]
    if expression.startswith("^.*"):
        expression = expression[len("^.*") :]
    return expression


def _regex(expression: str, *, ignore_case: bool) -> dict[str, Any]:
    # "s": a wildcard matches a line break too, as SQL's does.
    return {"$regex": expression, _REGEX_OPTIONS: "is" if ignore_case else "s"}


def like(pattern: Any, *, ignore_case: bool = False) -> dict[str, Any]:
    """The condition of ``LIKE pattern`` (anchored, case-sensitive unless *ignore_case*)."""
    return _regex(_like_expression(str(pattern)), ignore_case=ignore_case)


def literal(value: Any, *, anchor_start: bool, anchor_end: bool, ignore_case: bool = False) -> dict[str, Any]:
    """The condition of a string that contains *value* as it is (``anchor_start``: starts with it,
    ``anchor_end``: ends with it)."""
    expression = re.escape(str(value))
    if anchor_start:
        expression = "^" + expression
    if anchor_end:
        expression += r"\z"
    return _regex(expression, ignore_case=ignore_case)


def ignoring_case(value: Any) -> dict[str, Any]:
    """The condition of a string equal to *value* whatever the case (a non-string compares as it is)."""
    if not isinstance(value, str):
        return {"$eq": value}
    return literal(value, anchor_start=True, anchor_end=True, ignore_case=True)


def equals(value: Any) -> Any:
    """``= value`` (``IS NULL`` for ``None``, which matches a missing field too)."""
    return value


def not_equals(value: Any) -> dict[str, Any]:
    """``<> value`` with SQL semantics: false for a null or missing field (``IS NOT NULL`` for ``None``)."""
    if value is None:
        return {"$ne": None}
    return {"$nin": [value, None]}


def in_values(values: Any) -> dict[str, Any]:
    """``IN values`` (an empty collection matches nothing)."""
    return {"$in": listed(values)}


def not_in_values(values: Any) -> dict[str, Any]:
    """``NOT IN values`` with SQL semantics: false for a null or missing field; an empty collection matches
    every document, as on SQL."""
    items = listed(values)
    return {"$nin": [*items, None]} if items else {"$nin": []}


def is_not_null() -> dict[str, Any]:
    """``IS NOT NULL``: the field exists and is not null (``IS NULL`` is ``{field: None}``, which matches a
    missing field too)."""
    return {"$ne": None}


# ---------------------------------------------------------------------------------------------------------
# Negation with SQL's three-valued logic
# ---------------------------------------------------------------------------------------------------------


def negate(document: Mapping[str, Any]) -> dict[str, Any]:
    """The documents for which the filter *document* is false (not unknown), SQL's ``NOT``: see the module
    documentation. An empty filter (every document) stays empty."""
    if not document:
        return {}
    clauses = [_negate_entry(key, value) for key, value in document.items()]
    return _any_of(clauses)


def _negate_entry(key: str, value: Any) -> dict[str, Any]:
    if key == "$and":
        return _any_of([negate(item) for item in value])
    if key == "$or":
        return _all_of([negate(item) for item in value])
    if key == "$nor":
        return _any_of([dict(item) for item in value])
    if key.startswith("$"):
        return {"$nor": [{key: value}]}  # $expr, $text, $where...: MongoDB's own negation
    return _negate_field(key, value)


def _negate_field(field: str, condition: Any) -> dict[str, Any]:
    if not _is_operator_document(condition):
        # Equality (a missing field is null: {f: None} is IS NULL).
        return {field: {"$ne": None}} if condition is None else {field: {"$nin": [condition, None]}}
    operators: dict[str, Any] = dict(condition)
    if operators.get("$ne", object()) is None and len(operators) > 1:
        # A not-null guard beside other conditions: those are unknown for a null field already.
        del operators["$ne"]
    options = operators.pop(_REGEX_OPTIONS, None)
    clauses = [_negate_operator(field, operator, argument, options) for operator, argument in operators.items()]
    return _any_of(clauses)


def _negate_operator(field: str, operator: str, argument: Any, options: Any) -> dict[str, Any]:
    if operator == "$eq":
        return _negate_field(field, argument) if argument is not None else {field: {"$ne": None}}
    if operator == "$ne":
        return {field: None} if argument is None else {field: argument}
    if operator == "$in":
        values = listed(argument)
        return {field: {"$nin": [*values, None]}} if values else {}
    if operator == "$nin":
        values = [value for value in listed(argument) if value is not None]
        return {field: {"$in": values}}
    if operator in _COMPARISONS:
        return {field: {"$not": {operator: argument}, "$ne": None}}
    if operator == "$regex":
        expression: dict[str, Any] = {"$regex": argument}
        if options is not None:
            expression[_REGEX_OPTIONS] = options
        return {field: {"$not": expression, "$ne": None}}
    if operator == "$not":
        return {field: argument}
    if operator == "$exists":
        return {field: {"$exists": not argument}}
    return {"$nor": [{field: {operator: argument}}]}  # $elemMatch, $size, $all, $type...: MongoDB's own


def _is_operator_document(condition: Any) -> bool:
    return isinstance(condition, Mapping) and bool(condition) and all(str(key).startswith("$") for key in condition)


def _any_of(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    if any(not clause for clause in clauses):
        return {}  # an empty clause matches every document, and so does an OR that holds it
    return clauses[0] if len(clauses) == 1 else {"$or": clauses}


def _all_of(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    kept = [clause for clause in clauses if clause]
    if not kept:
        return {}
    return kept[0] if len(kept) == 1 else {"$and": kept}
