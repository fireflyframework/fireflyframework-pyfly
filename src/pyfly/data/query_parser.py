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
"""Derived query methods: the shared parser of their names (Spring's ``PartTree``) and of their return types.

Parses method names like ``find_by_status_and_role_order_by_name_desc`` into structured query descriptions,
and return annotations like ``-> User | None`` into the shape a query's result takes. Framework-agnostic: it
contains no backend imports. Each data adapter provides its own compiler via
:class:`~pyfly.data.ports.compiler.QueryMethodCompilerPort`.

Grammar
-------
**Prefixes:** ``find_by``, ``count_by``, ``exists_by``, ``delete_by``

**Connectors:** ``_and_``, ``_or_``. ``and`` binds tighter than ``or``, as in Spring and in SQL:
``find_by_a_or_b_and_c`` is ``a OR (b AND c)``. :attr:`ParsedQuery.groups` holds the ``or`` of ``and``
groups.

**Operators (suffix on field name):**
    - *(none)* = equals (default)
    - ``_greater_than`` = ``>``
    - ``_less_than`` = ``<``
    - ``_greater_than_equal`` = ``>=``
    - ``_less_than_equal`` = ``<=``
    - ``_between`` = BETWEEN (takes 2 args)
    - ``_like`` = LIKE (the argument is a pattern: its ``%`` and ``_`` are wildcards)
    - ``_containing`` = contains the argument as it is (its ``%`` and ``_`` are plain characters)
    - ``_in`` = IN (takes list arg)
    - ``_not`` = ``!=``
    - ``_is_null`` = IS NULL (no arg)
    - ``_is_not_null`` = IS NOT NULL (no arg)

**Ordering suffix:** ``_order_by_{field}_{asc|desc}`` (can chain multiple)

**Parsed against an entity's properties** (``parse(name, properties=...)``, what the repository
post-processors do at startup), the name must name properties of the entity, or the parse fails with
:class:`InvalidQueryMethodError`, and the property names decide how the name splits: a property called
``logged_in`` or ``terms_and_conditions_accepted`` is read whole (the longest property that fits wins),
never as ``logged`` IN or ``terms`` AND ``conditions_accepted``. That parse knows Spring's other keywords
too, each optionally preceded by ``is`` (``_is_between``, ``_is_in``):

    - ``_is``, ``_equals`` = equals; ``_is_not`` = ``!=``
    - ``_after`` = ``>``; ``_before`` = ``<``
    - ``_null`` / ``_not_null`` = IS NULL / IS NOT NULL
    - ``_true`` / ``_false`` = the boolean property is true / false (no arg)
    - ``_not_like``; ``_not_in``
    - ``_starting_with`` (``_starts_with``), ``_ending_with`` (``_ends_with``), ``_containing``
      (``_contains``), ``_not_containing``: the argument is matched as it is
    - ``_ignore_case`` (``_ignoring_case``) after a predicate compares that string property without case, and
      ``_all_ignore_case`` at the end of the criteria every string property of the query

Without the properties (``parse(name)``), the parser keeps the original keyword set above and splits on every
``_and_``/``_or_``.

Return types
------------
:func:`result_shape` reads a query method's return annotation: ``list[T]`` (and ``Sequence[T]``), ``T | None``
(or a bare ``T``: one result or ``None``), ``Page[T]``, ``Slice[T]``, ``None``, and scalars (``int``,
``bool``...); ``T`` is the entity, a :func:`~pyfly.data.projection.projection`, a scalar, a ``tuple`` (a row),
a ``dict`` (a row by column name), or another class built from a row's columns by name. A single-result
method whose query matches more than one row raises :class:`IncorrectResultSizeException`.

Example::

    parser = QueryMethodParser()
    parsed = parser.parse("find_by_status_and_role_order_by_name_desc")
    # -> ParsedQuery with predicates and order clauses
"""

from __future__ import annotations

import collections.abc
import datetime
import decimal
import enum
import re
import types
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar, Union, get_args, get_origin

from pyfly.data.page import Page, Slice
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.projection import is_projection
from pyfly.kernel.exceptions import PyFlyException

__all__ = [
    "CASE_FOLDING_OPERATORS",
    "KEYWORDS",
    "LIKE_OPERATORS",
    "OPERATORS",
    "OPERATOR_ARGUMENTS",
    "ElementKind",
    "FieldPredicate",
    "IncorrectResultSizeException",
    "InvalidQueryMethodError",
    "OrderClause",
    "ParsedQuery",
    "QueryMethodParser",
    "ResultKind",
    "ResultShape",
    "is_special_parameter",
    "result_shape",
    "value_parameters",
]

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
"""The operator suffixes of a parse without the entity's properties (the original keyword set)."""

KEYWORDS: dict[tuple[str, ...], str] = {
    ("greater", "than", "equal"): "gte",
    ("less", "than", "equal"): "lte",
    ("greater", "than"): "gt",
    ("less", "than"): "lt",
    ("after",): "gt",
    ("before",): "lt",
    ("between",): "between",
    ("not", "null"): "is_not_null",
    ("null",): "is_null",
    ("true",): "is_true",
    ("false",): "is_false",
    ("not", "like"): "not_like",
    ("like",): "like",
    ("starting", "with"): "starting_with",
    ("starts", "with"): "starting_with",
    ("ending", "with"): "ending_with",
    ("ends", "with"): "ending_with",
    ("not", "containing"): "not_containing",
    ("containing",): "containing",
    ("contains",): "containing",
    ("not", "in"): "not_in",
    ("in",): "in",
    ("not",): "not",
    ("equals",): "eq",
}
"""The keywords of a parse against the entity's properties, each optionally preceded by ``is``; a bare ``is``
is equals."""

OPERATOR_ARGUMENTS: dict[str, int] = {
    "between": 2,
    "is_null": 0,
    "is_not_null": 0,
    "is_true": 0,
    "is_false": 0,
}
"""How many method arguments an operator takes (one when it is not listed)."""

LIKE_OPERATORS = frozenset({"like", "not_like", "containing", "not_containing", "starting_with", "ending_with"})
"""The operators that match a string with ``LIKE`` (``like``/``not_like`` take a pattern, the others a value
matched as it is)."""

CASE_FOLDING_OPERATORS = frozenset({"eq", "not", *LIKE_OPERATORS})
"""The operators ``_ignore_case`` applies to."""

_IGNORE_CASE: tuple[tuple[str, ...], ...] = (("ignore", "case"), ("ignoring", "case"))
_ALL_IGNORE_CASE: tuple[tuple[str, ...], ...] = (("all", "ignore", "case"), ("all", "ignoring", "case"))
_CONNECTORS = ("and", "or")
_DIRECTIONS = ("asc", "desc")

# Keyword sequences, longest first, each also after "is" (a bare "is" is equals).
_KEYWORD_SEQUENCES: list[tuple[tuple[str, ...], str]] = sorted(
    [*KEYWORDS.items(), *((("is", *words), operator) for words, operator in KEYWORDS.items()), (("is",), "eq")],
    key=lambda item: -len(item[0]),
)


class InvalidQueryMethodError(ValueError):
    """A derived or ``@query`` method that cannot be implemented as declared: a name that does not parse, a
    property the entity does not have, parameters that do not match the query, an unsupported return type.

    Raised when the repository is built (at startup), as Spring rejects such a method at bootstrap, never on
    the first call.
    """


class IncorrectResultSizeException(PyFlyException):
    """A single-result query method (``-> T | None``) matched more than one row (Spring's
    ``IncorrectResultSizeDataAccessException``)."""

    def __init__(self, message: str, *, expected: int, actual: int) -> None:
        super().__init__(message, code="INCORRECT_RESULT_SIZE", context={"expected": expected, "actual": actual})
        self.expected = expected
        self.actual = actual


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FieldPredicate:
    """A single field predicate parsed from a method name."""

    field_name: str
    operator: str = "eq"  # default is equals
    ignore_case: bool = False
    """Whether the name asked for this predicate to ignore case (``_ignore_case``)."""

    @property
    def arguments(self) -> int:
        """How many method arguments the predicate takes."""
        return OPERATOR_ARGUMENTS.get(self.operator, 1)


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
    all_ignore_case: bool = False
    """Whether the name ends its criteria with ``_all_ignore_case`` (every string predicate ignores case)."""

    @property
    def groups(self) -> list[list[FieldPredicate]]:
        """The predicates as an ``or`` of ``and`` groups: ``and`` binds tighter than ``or`` (Spring's
        ``PartTree``), so ``a_or_b_and_c`` is ``[[a], [b, c]]``. Empty without predicates."""
        if not self.predicates:
            return []
        groups: list[list[FieldPredicate]] = [[self.predicates[0]]]
        for connector, predicate in zip(self.connectors, self.predicates[1:], strict=False):
            if connector == "or":
                groups.append([predicate])
            else:
                groups[-1].append(predicate)
        return groups

    @property
    def argument_count(self) -> int:
        """How many method arguments the predicates take, in order."""
        return sum(predicate.arguments for predicate in self.predicates)


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

    def parse(self, method_name: str, *, properties: Collection[str] | None = None) -> ParsedQuery:
        """Parse a method name into a :class:`ParsedQuery`.

        With *properties* (the entity's property names), every field must be one of them, the names decide
        how the method name splits, and the full keyword set applies (module documentation); a name that
        does not read that way raises :class:`InvalidQueryMethodError`.
        """
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

        if properties is not None:
            return self._parse_against(method_name, prefix, body, frozenset(properties))

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
    # Parsing against the entity's properties
    # ------------------------------------------------------------------

    def _parse_against(self, method_name: str, prefix: str, body: str, properties: frozenset[str]) -> ParsedQuery:
        words = body.split("_") if body else []
        if any(not word for word in words):
            raise InvalidQueryMethodError(f"{method_name}: a name part is empty (two underscores in a row)")
        # The ORDER BY starts at the first "order_by" after which both parts read as properties.
        starts = [index for index in range(len(words) - 1) if words[index] == "order" and words[index + 1] == "by"]
        for start in starts:
            order_words = words[start + 2 :]
            orders = _segment_orders(order_words, properties) if order_words else None
            if orders is None:
                continue
            criteria = _parse_criteria(words[:start], properties)
            if criteria is not None:
                predicates, connectors, all_ignore_case = criteria
                return ParsedQuery(prefix, predicates, connectors, orders, all_ignore_case)
        criteria = _parse_criteria(words, properties)
        if criteria is None:
            raise InvalidQueryMethodError(_explain(method_name, body, properties))
        predicates, connectors, all_ignore_case = criteria
        return ParsedQuery(prefix, predicates, connectors, [], all_ignore_case)

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


def _parse_criteria(
    words: list[str], properties: frozenset[str]
) -> tuple[list[FieldPredicate], list[str], bool] | None:
    """The predicates and connectors *words* read as against *properties* (``None`` when they do not), and
    whether they end with ``all_ignore_case``."""
    all_ignore_case = False
    for suffix in _ALL_IGNORE_CASE:
        if len(words) > len(suffix) and tuple(words[-len(suffix) :]) == suffix:
            words, all_ignore_case = words[: -len(suffix)], True
            break
    if not words:
        return ([], [], False) if not all_ignore_case else None
    found = _segment_predicates(words, 0, properties)
    if found is None:
        return None
    predicates = [predicate for predicate, _connector in found]
    connectors = [connector for _predicate, connector in found[:-1]]
    return predicates, connectors, all_ignore_case


def _segment_predicates(
    words: list[str], start: int, properties: frozenset[str]
) -> list[tuple[FieldPredicate, str]] | None:
    """Read ``words[start:]`` as predicates joined by connectors: the longest property first, then the longest
    keyword, backtracking when the rest does not read. Each predicate comes with the connector after it (an
    empty string for the last)."""
    for end in range(len(words), start, -1):
        name = "_".join(words[start:end])
        if name not in properties:
            continue
        for length, operator in (*_keywords_at(words, end), (0, "eq")):
            after = end + length
            for folds in (True, False):
                position = after
                if folds:
                    fold = next((len(s) for s in _IGNORE_CASE if tuple(words[after : after + len(s)]) == s), 0)
                    if not fold:
                        continue
                    position = after + fold
                predicate = FieldPredicate(name, operator, ignore_case=folds)
                if position == len(words):
                    return [(predicate, "")]
                if words[position] in _CONNECTORS and position + 1 < len(words):
                    rest = _segment_predicates(words, position + 1, properties)
                    if rest is not None:
                        return [(predicate, words[position]), *rest]
    return None


def _keywords_at(words: list[str], position: int) -> list[tuple[int, str]]:
    """The keywords that start at *position*, longest first, as ``(word count, operator)``."""
    return [
        (len(sequence), operator)
        for sequence, operator in _KEYWORD_SEQUENCES
        if tuple(words[position : position + len(sequence)]) == sequence
    ]


def _segment_orders(words: list[str], properties: frozenset[str]) -> list[OrderClause] | None:
    """Read *words* as ``property [asc|desc]`` repeated (the longest property first), or ``None``."""
    if not words:
        return []
    for end in range(len(words), 0, -1):
        name = "_".join(words[:end])
        if name not in properties:
            continue
        direction = "asc"
        rest = end
        if rest < len(words) and words[rest] in _DIRECTIONS:
            direction = words[rest]
            rest += 1
        following = _segment_orders(words[rest:], properties)
        if following is not None:
            return [OrderClause(name, direction), *following]
    return None


def _explain(method_name: str, body: str, properties: frozenset[str]) -> str:
    """Why *body* does not read as predicates of *properties*: the parts that name no property."""
    listing = ", ".join(sorted(properties)[:20]) + (", ..." if len(properties) > 20 else "")
    criteria = re.split(r"_order_by_", body, maxsplit=1)
    unknown: list[str] = []
    for segment in re.split(r"_and_|_or_", criteria[0]) if criteria[0] else []:
        predicate = QueryMethodParser._parse_single_predicate(segment)
        if segment not in properties and predicate.field_name not in properties:
            unknown.append(segment)
    if len(criteria) > 1 and _segment_orders(criteria[1].split("_"), properties) is None:
        unknown.append(f"order_by_{criteria[1]}")
    detail = f"{', '.join(repr(part) for part in unknown)} names no property" if unknown else "it does not parse"
    return (
        f"{method_name} cannot be read as a derived query: {detail} (properties joined by _and_/_or_, each with an "
        f"optional keyword such as _greater_than or _in, then _order_by_<property>[_asc|_desc]); properties: {listing}"
    )


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


class ResultKind(enum.Enum):
    """How many results a query method returns, and in what container."""

    LIST = "list"
    """``list[T]`` (``Sequence[T]``, ``Iterable[T]``): every result."""
    ONE = "one"
    """``T | None`` or ``T``: the one result, or ``None`` when there is none."""
    PAGE = "page"
    """``Page[T]``: a page of results and the total (the method takes a ``Pageable``)."""
    SLICE = "slice"
    """``Slice[T]``: a page of results and whether another follows (the method takes a ``Pageable``)."""
    NONE = "none"
    """``None``: nothing."""


class ElementKind(enum.Enum):
    """What each result of a query method is."""

    ENTITY = "entity"
    """The repository's entity (also for an unannotated result, ``Any`` or a type variable)."""
    PROJECTION = "projection"
    """A :func:`~pyfly.data.projection.projection`: an object with the projection's fields."""
    SCALAR = "scalar"
    """A single value: ``int``, ``bool``, ``str``, ``float``, ``Decimal``, a date or time, a ``UUID``, an enum."""
    ROW = "row"
    """A ``tuple`` of the row's values."""
    MAPPING = "mapping"
    """A ``dict`` of the row's values by column name."""
    OBJECT = "object"
    """Another class: an adapter reads a mapped entity class as one, and builds any other from the row's
    columns by name."""


_SCALARS: tuple[type, ...] = (
    bool,
    int,
    float,
    str,
    bytes,
    decimal.Decimal,
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
    enum.Enum,
)

_LIST_ORIGINS: frozenset[Any] = frozenset(
    {
        list,
        collections.abc.Sequence,
        collections.abc.Iterable,
        collections.abc.Collection,
        collections.abc.MutableSequence,
    }
)


@dataclass(frozen=True)
class ResultShape:
    """The shape of a query method's result, read from its return annotation (:func:`result_shape`)."""

    kind: ResultKind
    element: ElementKind
    type: Any = None
    """The element's type: the entity, the projection, ``int``... (``None`` when the annotation names none)."""


def result_shape(annotation: Any, entity: type | None = None) -> ResultShape:
    """The :class:`ResultShape` of a query method returning *annotation* (a resolved type hint), for
    *entity*. Raises :class:`InvalidQueryMethodError` for an annotation that no query result has (a union of
    two types, an unparametrized mapping of lists...)."""
    if annotation is None or annotation is type(None):
        return ResultShape(ResultKind.NONE, ElementKind.ENTITY, None)
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [member for member in get_args(annotation) if member is not type(None)]
        if len(members) != 1:
            raise InvalidQueryMethodError(f"A query method returns one type (or it or None), not {annotation}")
        return result_shape(members[0], entity)
    if (origin is not None and origin in _LIST_ORIGINS) or annotation in _LIST_ORIGINS:
        return ResultShape(ResultKind.LIST, *_element(_first_argument(annotation), entity))
    if origin is Page or annotation is Page:
        return ResultShape(ResultKind.PAGE, *_element(_first_argument(annotation), entity))
    if origin is Slice or annotation is Slice:
        return ResultShape(ResultKind.SLICE, *_element(_first_argument(annotation), entity))
    return ResultShape(ResultKind.ONE, *_element(annotation, entity))


def _first_argument(annotation: Any) -> Any:
    arguments = get_args(annotation)
    return arguments[0] if arguments else None


def _element(element: Any, entity: type | None) -> tuple[ElementKind, Any]:
    if element is None or element is Any or isinstance(element, TypeVar):
        return ElementKind.ENTITY, entity
    origin = get_origin(element)
    if origin is tuple or element is tuple:
        return ElementKind.ROW, tuple
    if origin in (dict, collections.abc.Mapping) or element in (dict, collections.abc.Mapping):
        return ElementKind.MAPPING, dict
    if not isinstance(element, type):
        raise InvalidQueryMethodError(f"A query method cannot return {element!r}")
    if entity is not None and issubclass(element, entity):
        return ElementKind.ENTITY, element
    if is_projection(element):
        return ElementKind.PROJECTION, element
    if issubclass(element, _SCALARS):
        return ElementKind.SCALAR, element
    if issubclass(element, tuple):
        return ElementKind.ROW, element
    if issubclass(element, collections.abc.Mapping):
        return ElementKind.MAPPING, element
    return ElementKind.OBJECT, element


def is_special_parameter(annotation: Any) -> bool:
    """Whether a parameter annotated *annotation* is a ``Pageable`` or a ``Sort`` (``| None`` included): a
    query method's special parameter, which pages or sorts the query instead of binding a value."""
    if get_origin(annotation) in (Union, types.UnionType):
        members = [member for member in get_args(annotation) if member is not type(None)]
        return len(members) == 1 and is_special_parameter(members[0])
    return isinstance(annotation, type) and issubclass(annotation, (Pageable, Sort))


def value_parameters(parameters: Sequence[Any]) -> int:
    """How many of *parameters* (``inspect.Parameter`` objects after ``self``, their annotations resolved)
    bind query values: those that are not special (:func:`is_special_parameter`)."""
    return sum(1 for parameter in parameters if not is_special_parameter(parameter.annotation))
