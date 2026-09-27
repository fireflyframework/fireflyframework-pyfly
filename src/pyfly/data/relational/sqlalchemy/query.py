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
"""SQLAlchemy ``@Query`` executor and JPQL transpiler.

Provides :class:`QueryExecutor` to compile ``@query``-decorated repository
methods into executable async callables backed by SQLAlchemy.

The :func:`query` decorator itself is backend-neutral and lives in
:mod:`pyfly.data.query`.  It is re-exported here for backward compatibility::

    from pyfly.data.relational.sqlalchemy.query import query   # still works
    from pyfly.data.query import query                         # preferred

Usage::

    from pyfly.data.query import modifying, query

    class UserRepository(Repository[User]):

        @query("SELECT u FROM User u WHERE u.email LIKE :pattern AND u.active = true")
        async def find_active_by_email_pattern(self, pattern: str) -> list[User]: ...

        @query("SELECT COUNT(u) FROM User u WHERE u.role = :role")
        async def count_by_role(self, role: str) -> int: ...

        @query("SELECT * FROM users WHERE email = :email", native=True)
        async def find_by_email_native(self, email: str) -> User | None: ...

        @modifying
        @query("UPDATE User u SET u.active = false WHERE u.last_login < :cutoff")
        async def deactivate_idle(self, cutoff: datetime) -> int: ...

**Results** follow the method's return annotation, never the SQL's text:

- the entity (``list[User]``, ``User | None``): the query runs as ``select(User).from_statement(text(sql))``,
  so its rows are the unit of work's own entities (identity-mapped: a change to one is flushed with the unit,
  ``save()`` updates it), typed and mapped by attribute through the entity's columns, their relationships
  loaded as any read loads them, and the unit's pending changes are flushed before it runs;
- a scalar (``int``, ``bool``, ``str``, ``list[str]``...), a row (``tuple``), a row by column name (``dict``),
  a :func:`~pyfly.data.projection.projection` or any other class built from the row's columns by name: the
  statement runs as it is, after the unit's pending changes are flushed;
- one result (``X | None``) is ``None`` when no row matches and raises
  :class:`~pyfly.data.query_parser.IncorrectResultSizeException` when more than one does;
- an unannotated method returns the entities, or the value of a query that starts with ``SELECT COUNT`` or
  ``SELECT EXISTS``.

**Statements that change rows** (``UPDATE``, ``DELETE``, ``INSERT``) need :func:`~pyfly.data.query.modifying`
and return the number of rows they changed (``None`` for ``-> None``); without it the repository fails to
build. **Arguments** bind by name (``:name``, a parameter of the method) and, in JPQL, by position (``?1`` is
the first parameter after ``self``); the method takes them by position or by keyword. ``IN (:ids)`` (or
``IN :ids``) binds a collection, one value per element. A ``UUID`` binds as SQLAlchemy's ``Uuid`` and an
aware ``datetime`` as ``UtcDateTime``, as entity columns of those types store them; other values bind as the
driver takes them.

**JPQL** (``native=False``, the default) is rewritten token by token, for the dialect the query runs on:

1. ``FROM Entity alias`` (and ``JOIN``, ``UPDATE``) names the entity's table (quoted where the dialect needs it);
   the alias stays, so correlated subqueries keep their correlation;
2. ``SELECT alias`` selects the entity's columns (``alias.col1, alias.col2, ...``); ``COUNT(alias)`` is
   ``COUNT(*)``;
3. ``alias.attribute`` names the attribute's column (``alias.column``); a name that is neither an attribute nor a
   column of the entity fails when the repository is built;
4. the literals ``true`` and ``false`` are what the dialect accepts (``true``/``false``, or ``1``/``0`` where
   booleans are integers); ``IS TRUE`` and ``IS FALSE`` are left as written;
5. ``UPDATE`` and ``DELETE`` drop the alias, and qualify a reference with the table (the ``SET`` targets stay
   bare, as PostgreSQL requires).

String literals, quoted identifiers and comments are never rewritten. A name that is not an entity (a table in a
subquery, ``schema.table``) is left as it is. The query's colons inside string literals are escaped, so
``'10:30'`` or ``'a:b'`` never become parameters.
"""

from __future__ import annotations

import inspect
import re
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, TypeVar, get_type_hints

from sqlalchemy import Uuid, bindparam, column, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ColumnProperty, Mapper
from sqlalchemy.types import NullType

from pyfly.data.projection import projection_fields
from pyfly.data.query import ModifyingOptions, query
from pyfly.data.query_parser import (
    ElementKind,
    IncorrectResultSizeException,
    InvalidQueryMethodError,
    ResultKind,
    ResultShape,
    is_special_parameter,
    result_shape,
)
from pyfly.data.relational.sqlalchemy.statements import unique_entities
from pyfly.data.relational.sqlalchemy.types import UtcDateTime

T = TypeVar("T")

__all__ = ["CompiledQuery", "QueryExecutor", "TranspiledQuery", "query", "tokenize", "transpile_jpql"]


# ---------------------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------------------

_TOKENS = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:[^']|'')*')
    | (?P<quoted>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\])
    | (?P<cast>::)
    | (?P<bind>:[A-Za-z_][A-Za-z0-9_]*)
    | (?P<positional>\?[0-9]+)
    | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<number>[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

_CLAUSE_KEYWORDS = frozenset(
    {
        "WHERE",
        "SET",
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "OUTER",
        "CROSS",
        "NATURAL",
        "ON",
        "USING",
        "GROUP",
        "ORDER",
        "HAVING",
        "LIMIT",
        "OFFSET",
        "FETCH",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        "FOR",
        "WINDOW",
        "RETURNING",
        "VALUES",
    }
)
"""Words that follow a table in a ``FROM`` clause and so are never its alias."""

_DML = frozenset({"UPDATE", "DELETE", "INSERT", "MERGE", "REPLACE", "UPSERT"})
_BOOLEANS = {"true": True, "false": False}


@dataclass
class Token:
    """A lexical token of a query: its kind (``space``, ``comment``, ``string``, ``quoted``, ``cast``, ``bind``,
    ``positional``, ``word``, ``number``, ``other``) and its text."""

    kind: str
    text: str

    @property
    def upper(self) -> str:
        return self.text.upper()


def tokenize(sql: str) -> list[Token]:
    """The tokens of *sql* (their texts concatenate back to it). An unterminated string literal or quoted
    identifier raises :class:`~pyfly.data.query_parser.InvalidQueryMethodError`."""
    tokens: list[Token] = []
    for match in _TOKENS.finditer(sql):
        kind = match.lastgroup or "other"
        value = match.group()
        if kind == "other" and value in ("'", '"', "`"):
            raise InvalidQueryMethodError(f"Unterminated {value} in the query: {sql}")
        tokens.append(Token(kind, value))
    return tokens


def _significant(tokens: Sequence[Token]) -> list[int]:
    return [index for index, token in enumerate(tokens) if token.kind not in ("space", "comment")]


def _statement_kind(tokens: Sequence[Token]) -> str:
    """The query's first keyword, upper-cased (``SELECT``, ``WITH``, ``UPDATE``...), past leading parentheses."""
    for index in _significant(tokens):
        token = tokens[index]
        if token.kind == "word":
            return token.upper
        if token.text != "(":
            break
    return ""


def _binds(tokens: Sequence[Token]) -> list[str]:
    """The named parameters of the query (``:name``, not ``::`` casts or ``\\:`` escapes), in order."""
    names: list[str] = []
    for index, token in enumerate(tokens):
        if token.kind == "bind" and not (index and tokens[index - 1].text == "\\"):
            names.append(token.text[1:])
    return names


def _expanding(tokens: list[Token]) -> set[str]:
    """Rewrite ``IN (:name)`` to ``IN :name`` in place, and return the names bound after ``IN``: those bind a
    collection, one value per element."""
    expanding: set[str] = set()
    significant = _significant(tokens)
    for position, index in enumerate(significant):
        token = tokens[index]
        if token.kind != "bind" or position == 0:
            continue
        before = tokens[significant[position - 1]]
        if before.kind == "word" and before.upper == "IN":
            expanding.add(token.text[1:])
            continue
        if (
            before.text == "("
            and position >= 2
            and tokens[significant[position - 2]].upper == "IN"
            and position + 1 < len(significant)
            and tokens[significant[position + 1]].text == ")"
        ):
            expanding.add(token.text[1:])
            tokens[significant[position - 1]].text = ""
            tokens[significant[position + 1]].text = ""
    return expanding


def _escaped_literals(tokens: Sequence[Token]) -> None:
    """Escape the colons of string literals in place, so ``text()`` never reads one as a parameter."""
    for token in tokens:
        if token.kind == "string" and ":" in token.text:
            token.text = token.text.replace(":", "\\:")


# ---------------------------------------------------------------------------------------------------------
# JPQL
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TranspiledQuery:
    """A query ready for ``text()``: its SQL for one dialect, and what binding it needs."""

    sql: str
    kind: str
    """The statement's first keyword (``SELECT``, ``WITH``, ``UPDATE``, ``DELETE``, ``INSERT``...)."""
    binds: tuple[str, ...]
    """The named parameters it binds, in order of appearance (with repeats)."""
    expanding: frozenset[str]
    """The parameters that bind a collection (``IN :name``)."""
    columns: tuple[tuple[str, Any], ...] = ()
    """The result columns' labels and SQL types, when the select list names entity attributes only."""

    @property
    def is_dml(self) -> bool:
        """Whether the statement changes rows."""
        return self.kind in _DML


class _Jpql:
    """The token rewrite of one JPQL query for one dialect (module documentation)."""

    def __init__(self, jpql: str, entity: type, dialect: Dialect | None, positional: Sequence[str]) -> None:
        self._jpql = jpql
        self._entity = entity
        self._dialect = dialect
        self._positional = positional
        self._tokens = tokenize(jpql)
        self._significant = _significant(self._tokens)
        self._mappers = _entities_of(entity)
        self._aliases: dict[str, Mapper[Any]] = {}
        self._tables: dict[int, Mapper[Any]] = {}
        self._alias_tokens: set[int] = set()
        self.kind = _statement_kind(self._tokens)

    def transpile(self) -> TranspiledQuery:
        self._declarations()
        columns = self._typed_columns()
        self._rewrite()
        _escaped_literals(self._tokens)
        expanding = _expanding(self._tokens)
        return TranspiledQuery(
            sql="".join(token.text for token in self._tokens),
            kind=self.kind,
            binds=tuple(_binds(self._tokens)),
            expanding=frozenset(expanding),
            columns=columns,
        )

    # -- helpers ------------------------------------------------------------------------------------------

    def _fail(self, message: str) -> InvalidQueryMethodError:
        return InvalidQueryMethodError(f"{message} (in the query {self._jpql!r})")

    def _next(self, index: int) -> int | None:
        """The index of the significant token after *index*, or ``None``."""
        position = self._significant.index(index) + 1
        return self._significant[position] if position < len(self._significant) else None

    def _previous(self, index: int) -> int | None:
        position = self._significant.index(index) - 1
        return self._significant[position] if position >= 0 else None

    def _text(self, index: int | None) -> str:
        return self._tokens[index].text if index is not None else ""

    def _upper(self, index: int | None) -> str:
        return self._tokens[index].upper if index is not None else ""

    def _quote(self, name: str) -> str:
        return self._dialect.identifier_preparer.quote(name) if self._dialect is not None else name

    def _table(self, mapper: Mapper[Any]) -> str:
        table: Any = mapper.local_table
        if self._dialect is not None:
            return str(self._dialect.identifier_preparer.format_table(table))
        return f"{table.schema}.{table.name}" if table.schema else str(table.name)

    # -- pass 1: FROM Entity alias ------------------------------------------------------------------------

    def _declarations(self) -> None:
        for index in self._significant:
            token = self._tokens[index]
            mapper = self._mappers.get(token.text) if token.kind == "word" else None
            if mapper is None:
                continue
            before = self._previous(index)
            if self._upper(before) not in ("FROM", "JOIN", "UPDATE") and self._text(before) != ",":
                continue
            self._tables[index] = mapper
            alias = self._next(index)
            if self._upper(alias) == "AS":
                self._alias_tokens.add(cast_index(alias))
                alias = self._next(cast_index(alias))
            if alias is None or self._tokens[alias].kind != "word" or self._upper(alias) in _CLAUSE_KEYWORDS:
                continue
            name = self._tokens[alias].text.lower()
            known = self._aliases.get(name)
            if known is not None and known is not mapper:
                raise self._fail(f"the alias {self._tokens[alias].text!r} names two entities")
            self._aliases[name] = mapper
            self._alias_tokens.add(alias)

    # -- the select list's types --------------------------------------------------------------------------

    def _typed_columns(self) -> tuple[tuple[str, Any], ...]:
        """The labels and types of the outermost select list when it names entity attributes only
        (``a.name``, ``a.name AS label``), so SQLite returns them typed too; ``()`` otherwise."""
        if self.kind != "SELECT":
            return ()
        items: list[list[int]] = [[]]
        depth = 0
        for index in self._significant[1:]:  # after SELECT
            text_ = self._tokens[index].text
            if depth == 0 and self._tokens[index].upper == "FROM":
                break
            if text_ == "(":
                depth += 1
            elif text_ == ")":
                depth -= 1
            if depth == 0 and text_ == ",":
                items.append([])
                continue
            items[-1].append(index)
        else:
            return ()  # no FROM: not a select list this can read
        columns: list[tuple[str, Any]] = []
        for item in items:
            words = [self._tokens[index] for index in item]
            if len(words) not in (3, 4, 5) or words[1].text != "." or words[0].text.lower() not in self._aliases:
                return ()
            mapper = self._aliases[words[0].text.lower()]
            attribute = mapper.attrs.get(words[2].text)
            if not isinstance(attribute, ColumnProperty) or len(attribute.columns) != 1:
                return ()
            if len(words) == 3:
                label = str(attribute.columns[0].name)
            elif len(words) == 4 and words[3].kind == "word":
                label = words[3].text
            elif len(words) == 5 and words[3].upper == "AS" and words[4].kind == "word":
                label = words[4].text
            else:
                return ()
            columns.append((label, attribute.columns[0].type))
        return tuple(columns)

    # -- pass 2: the rewrite ------------------------------------------------------------------------------

    def _rewrite(self) -> None:
        dml = self.kind in ("UPDATE", "DELETE")
        in_set = False
        depth = 0
        for index in list(self._significant):
            token = self._tokens[index]
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            if token.kind == "word" and depth == 0 and token.upper in ("SET", "WHERE", "RETURNING"):
                in_set = token.upper == "SET"
            if index in self._tables:
                token.text = self._table(self._tables[index])
                continue
            if dml and index in self._alias_tokens:
                token.text = ""
                if self._tokens[index - 1].kind == "space":
                    self._tokens[index - 1].text = ""
                continue
            if token.kind == "positional":
                self._positional_bind(token)
                continue
            if token.kind == "word" and token.text.lower() in _BOOLEANS:
                self._boolean(index, token)
                continue
            if token.kind == "word" and token.text.lower() in self._aliases and index not in self._alias_tokens:
                self._reference(index, token, dml=dml, in_set=in_set and depth == 0)

    def _positional_bind(self, token: Token) -> None:
        position = int(token.text[1:])
        if not 1 <= position <= len(self._positional):
            raise self._fail(
                f"{token.text} names parameter {position}, and the method has {len(self._positional)} after self"
            )
        token.kind, token.text = "bind", f":{self._positional[position - 1]}"

    def _boolean(self, index: int, token: Token) -> None:
        before, after = self._previous(index), self._next(index)
        if self._text(before) == "." or self._text(after) in (".", "("):
            return  # a column or a function called true/false
        negated = self._upper(before) == "NOT" and before is not None
        if self._upper(before) == "IS" or (negated and self._upper(self._previous(cast_index(before))) == "IS"):
            return  # IS TRUE, IS NOT FALSE: a predicate of its own
        if self._dialect is not None and not self._dialect.supports_native_boolean:
            token.text = "1" if _BOOLEANS[token.text.lower()] else "0"

    def _reference(self, index: int, token: Token, *, dml: bool, in_set: bool) -> None:
        mapper = self._aliases[token.text.lower()]
        dot = self._next(index)
        if self._text(dot) == ".":
            target = self._next(cast_index(dot))
            if target is None:
                raise self._fail(f"{token.text}. names nothing")
            if self._text(target) == "*":
                return
            name = self._column(mapper, self._tokens[target].text)
            self._tokens[target].text = self._quote(name)
            if dml:
                if in_set and self._text(self._next(target)) == "=":
                    token.text, self._tokens[cast_index(dot)].text = "", ""  # SET col = ...: the target stays bare
                else:
                    token.text = self._table(mapper)
            return
        if self._text(self._previous(index)) == ".":
            return
        self._bare_alias(index, token, mapper)

    def _column(self, mapper: Mapper[Any], name: str) -> str:
        attribute = mapper.attrs.get(name)
        if isinstance(attribute, ColumnProperty) and len(attribute.columns) == 1:
            return str(attribute.columns[0].name)
        if name in mapper.local_table.c:
            return name
        raise self._fail(f"{mapper.class_.__name__} has no attribute or column {name!r}")

    def _bare_alias(self, index: int, token: Token, mapper: Mapper[Any]) -> None:
        """``SELECT a`` -> the entity's columns; ``COUNT(a)`` -> ``COUNT(*)``; ``COUNT(DISTINCT a)`` -> the key."""
        before = self._previous(index)
        after = self._next(index)
        counted = self._text(before) == "(" and self._text(after) == ")"
        if counted and self._upper(self._previous(cast_index(before))) == "COUNT":
            token.text = "*"
            return
        if self._upper(before) == "DISTINCT" and self._text(after) == ")":
            keys = list(mapper.primary_key)
            if len(keys) != 1:
                raise self._fail(f"COUNT(DISTINCT {token.text}) needs a single-column key")
            token.text = f"{token.text}.{self._quote(str(keys[0].name))}"
            return
        if self._upper(before) in ("SELECT", "DISTINCT", "ALL") or self._text(before) == ",":
            columns = [f"{token.text}.{self._quote(str(col.name))}" for col in mapper.local_table.columns]
            token.text = ", ".join(columns)
            return
        raise self._fail(
            f"the alias {token.text!r} is used as a value; compare one of its attributes instead "
            f"({token.text}.{mapper.get_property_by_column(list(mapper.primary_key)[0]).key})"
        )


def cast_index(index: int | None) -> int:
    """*index*, known not to be ``None`` here."""
    if index is None:  # pragma: no cover — callers check the token exists first
        raise AssertionError("missing token")
    return index


def _entities_of(entity: type) -> dict[str, Mapper[Any]]:
    """The mapped classes a JPQL query of *entity* may name, by class name (the entity's own name wins)."""
    mapper: Mapper[Any] = sa_inspect(entity)
    mappers = {other.class_.__name__: other for other in mapper.registry.mappers}
    mappers[entity.__name__] = mapper
    return mappers


def transpile_jpql(
    jpql: str, entity: type, dialect: Dialect | None = None, *, positional: Sequence[str] = ()
) -> TranspiledQuery:
    """The SQL of *jpql* for *dialect* (module documentation); ``?N`` names ``positional[N - 1]``. Without a
    dialect, names are not quoted and boolean literals stay ``true``/``false``."""
    return _Jpql(jpql, entity, dialect, positional).transpile()


def _native(sql: str) -> TranspiledQuery:
    tokens = tokenize(sql)
    kind = _statement_kind(tokens)
    _escaped_literals(tokens)
    expanding = _expanding(tokens)
    return TranspiledQuery(
        sql="".join(token.text for token in tokens),
        kind=kind,
        binds=tuple(_binds(tokens)),
        expanding=frozenset(expanding),
    )


# ---------------------------------------------------------------------------------------------------------
# Compiled queries
# ---------------------------------------------------------------------------------------------------------

_LEGACY_SCALAR = re.compile(r"^\s*SELECT\s+(COUNT|EXISTS)\b", re.IGNORECASE)
_CONVERTIBLE: tuple[type, ...] = (bool, int, float, str)


class CompiledQuery:
    """A compiled ``@query`` method (module documentation): ``await compiled(session, **arguments)``, with the
    method's arguments by name."""

    def __init__(
        self,
        method: Callable[..., Any],
        entity: type,
        *,
        name: str,
        parameters: Sequence[str],
        return_type: Any,
    ) -> None:
        self._entity = entity
        self._name = name
        self._native: bool = bool(getattr(method, "__pyfly_query_native__", False))
        self._query: str = method.__pyfly_query__  # type: ignore[attr-defined]
        self._parameters = tuple(parameters)
        self._modifying: ModifyingOptions | None = getattr(method, "__pyfly_modifying__", None)
        self._by_dialect: dict[str, tuple[TranspiledQuery, Any]] = {}
        self._lock = threading.Lock()
        described = self._transpile(None)  # checks the query once, at startup, whatever the dialect
        self._kind = described.kind
        self._check(described)
        self._shape = self._shape_of(described, return_type)

    # -- compilation ------------------------------------------------------------------------------------

    @property
    def is_modifying(self) -> bool:
        """Whether the statement changes rows (``@modifying``)."""
        return self._modifying is not None

    @property
    def shape(self) -> ResultShape:
        """The shape of the result (from the return annotation)."""
        return self._shape

    def _fail(self, message: str) -> InvalidQueryMethodError:
        return InvalidQueryMethodError(f"{self._name}: {message}")

    def _transpile(self, dialect: Dialect | None) -> TranspiledQuery:
        try:
            if self._native:
                return _native(self._query)
            return transpile_jpql(self._query, self._entity, dialect, positional=self._parameters)
        except InvalidQueryMethodError as error:
            raise self._fail(str(error)) from None

    def _check(self, described: TranspiledQuery) -> None:
        unknown = sorted(set(described.binds) - set(self._parameters))
        if unknown:
            raise self._fail(
                f"the query binds {', '.join(':' + name for name in unknown)}, and the method has no such parameter "
                f"(parameters: {', '.join(self._parameters) or 'none'})"
            )
        if described.is_dml and self._modifying is None:
            raise self._fail(
                f"a {described.kind} statement changes rows: mark the method @modifying (it returns the row count)"
            )
        if not described.is_dml and self._modifying is not None:
            raise self._fail(f"@modifying marks a statement that changes rows, and this one is a {described.kind}")

    def _shape_of(self, described: TranspiledQuery, return_type: Any) -> ResultShape:
        if self._modifying is not None:
            if return_type in (None, int, Any) or return_type is type(None):
                return (
                    ResultShape(ResultKind.NONE, ElementKind.SCALAR, None)
                    if return_type is type(None)
                    else ResultShape(ResultKind.ONE, ElementKind.SCALAR, int)
                )
            raise self._fail(f"a @modifying query returns int (the row count) or None, not {return_type}")
        if return_type is None or return_type is Any:
            if _LEGACY_SCALAR.match(described.sql):
                kind = _LEGACY_SCALAR.match(described.sql).group(1).upper()  # type: ignore[union-attr]
                return ResultShape(ResultKind.ONE, ElementKind.SCALAR, int if kind == "COUNT" else bool)
            return ResultShape(ResultKind.LIST, ElementKind.ENTITY, self._entity)
        try:
            shape = result_shape(return_type, self._entity)
        except InvalidQueryMethodError as error:
            raise self._fail(str(error)) from None
        if shape.kind in (ResultKind.PAGE, ResultKind.SLICE):
            raise self._fail("a @query method returns a list or one result, not a Page or Slice; page a derived query")
        if shape.kind is ResultKind.NONE:
            raise self._fail("a query that is not @modifying returns what it selects, not None")
        if shape.element is ElementKind.OBJECT and _is_mapped(shape.type):
            return ResultShape(shape.kind, ElementKind.ENTITY, shape.type)
        return shape

    def _prepared(self, dialect: Dialect) -> tuple[TranspiledQuery, Any]:
        """The transpiled query and its ``text()`` clause for *dialect*, built on first use."""
        name = dialect.name
        prepared = self._by_dialect.get(name)
        if prepared is None:
            described = self._transpile(dialect)
            clause: Any = text(described.sql)
            if described.columns and self._shape.element is not ElementKind.ENTITY:
                clause = clause.columns(*(column(label, type_) for label, type_ in described.columns))
            with self._lock:
                prepared = self._by_dialect.setdefault(name, (described, clause))
        return prepared

    # -- execution ----------------------------------------------------------------------------------------

    async def __call__(self, session: AsyncSession, **arguments: Any) -> Any:
        dialect = session.sync_session.get_bind().dialect
        described, clause = self._prepared(dialect)
        values = {}
        for name in dict.fromkeys(described.binds):
            if name not in arguments:
                raise TypeError(f"{self._name}: the query needs a value for :{name}")
            values[name] = arguments[name]
        bound = _bound(clause, values, described.expanding) if values else clause
        if self._modifying is not None:
            return await self._modify(session, bound)
        if self._shape.element is ElementKind.ENTITY:
            entity = self._shape.type or self._entity
            result = await session.execute(select(entity).from_statement(bound))
            return self._shaped(unique_entities(result))
        await _flush_pending(session)
        result = await session.execute(bound)
        return self._shaped(self._rows(result))

    async def _modify(self, session: AsyncSession, statement: Any) -> Any:
        options = self._modifying or ModifyingOptions()
        if options.flush_automatically:
            await _flush_pending(session)
        result = await session.execute(statement)
        count = int(getattr(result, "rowcount", 0) or 0)
        if options.clear_automatically:
            session.expunge_all()
        return None if self._shape.kind is ResultKind.NONE else count

    def _rows(self, result: Any) -> list[Any]:
        element = self._shape.element
        target = self._shape.type
        if element is ElementKind.SCALAR:
            values = list(result.scalars().all())
            if target in _CONVERTIBLE:
                return [value if value is None or type(value) is target else target(value) for value in values]
            return values
        if element is ElementKind.ROW:
            return [tuple(row) for row in result.all()]
        mappings = [dict(row._mapping) for row in result.all()]
        if element is ElementKind.MAPPING:
            return mappings
        if element is ElementKind.PROJECTION:
            fields = projection_fields(target)
            return [SimpleNamespace(**{field: self._field(row, field) for field in fields}) for row in mappings]
        return [target(**row) for row in mappings]

    def _field(self, row: Mapping[str, Any], field: str) -> Any:
        if field not in row:
            raise KeyError(f"{self._name}: the query returns no column {field!r} (columns: {', '.join(row)})")
        return row[field]

    def _shaped(self, rows: list[Any]) -> Any:
        if self._shape.kind is not ResultKind.ONE:
            return rows
        if len(rows) > 1:
            raise IncorrectResultSizeException(
                f"{self._name} returns one result, and the query returned {len(rows)} rows",
                expected=1,
                actual=len(rows),
            )
        return rows[0] if rows else None


def _bound(clause: Any, values: dict[str, Any], expanding: frozenset[str]) -> Any:
    """*clause* with *values* bound: a ``UUID`` as SQLAlchemy's ``Uuid`` and an aware ``datetime`` as
    :class:`~pyfly.data.relational.sqlalchemy.types.UtcDateTime`, as entity columns of those types store them (a
    ``UUID`` is 32 hex digits on SQLite and MySQL); any other value as the driver takes it."""
    return clause.bindparams(*(_parameter(name, value, name in expanding) for name, value in values.items()))


def _parameter(name: str, value: Any, expanding: bool) -> Any:
    sample = value[0] if expanding and isinstance(value, Sequence) and value else value
    if isinstance(sample, uuid.UUID):
        return bindparam(name, value, type_=Uuid(), expanding=expanding)
    if isinstance(sample, datetime) and sample.tzinfo is not None:
        return bindparam(name, value, type_=UtcDateTime(), expanding=expanding)
    return bindparam(name, value, type_=NullType(), expanding=expanding)


async def _flush_pending(session: AsyncSession) -> None:
    """Flush the session's pending changes before a statement the ORM does not flush for (a raw one)."""
    if session.sync_session.autoflush and (session.new or session.deleted or session.dirty):
        await session.flush()


def _is_mapped(candidate: Any) -> bool:
    return isinstance(candidate, type) and isinstance(sa_inspect(candidate, raiseerr=False), Mapper)


class QueryExecutor:
    """Execute ``@query``-decorated methods against SQLAlchemy.

    This class is used by the ``RepositoryBeanPostProcessor`` to wire up
    query methods at startup time.  Given a decorated method and an entity
    type it produces an async callable that, when invoked with an
    :class:`~sqlalchemy.ext.asyncio.AsyncSession` and keyword arguments,
    executes the query and returns mapped results.
    """

    def compile_query_method(
        self,
        method: Callable[..., Any],
        entity: type[T],
        *,
        name: str | None = None,
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Compile a ``@query``-decorated method into an executable async function.

        Args:
            method: The decorated method (must have ``__pyfly_query__``).
            entity: The entity type used for result mapping and JPQL
                    transpilation.
            name: How messages name the method (default: its qualified name).

        Returns:
            A :class:`CompiledQuery`, called as ``(session: AsyncSession, **arguments)``, whose result follows
            the method's return annotation (module documentation).

        Raises:
            AttributeError: If *method* was not decorated with :func:`query`.
            InvalidQueryMethodError: If the method cannot be implemented as declared (an unknown parameter or
                attribute, a statement that changes rows without ``@modifying``, an unsupported return type).
        """
        if not hasattr(method, "__pyfly_query__"):
            raise AttributeError(f"{method} is not decorated with @query (missing __pyfly_query__)")
        function = inspect.unwrap(method)
        described: str = name or str(getattr(function, "__qualname__", repr(function)))
        try:
            hints = get_type_hints(function, localns={entity.__name__: entity})
        except Exception as error:  # noqa: BLE001 — any failure to evaluate an annotation is a declaration error
            raise InvalidQueryMethodError(f"{described}: its annotations do not resolve ({error})") from error
        signature = inspect.signature(function)
        parameters = list(signature.parameters.values())[1:]  # after self
        for parameter in parameters:
            if is_special_parameter(hints.get(parameter.name, parameter.annotation)):
                raise InvalidQueryMethodError(
                    f"{described}: a @query method takes no Pageable or Sort (page or sort a derived query, or write "
                    "ORDER BY and LIMIT in the query)"
                )
        return CompiledQuery(
            method,
            entity,
            name=described,
            parameters=[
                parameter.name
                for parameter in parameters
                if parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            ],
            return_type=hints.get("return"),
        )

    @staticmethod
    def _transpile_jpql(jpql: str, entity: type, dialect: Dialect | None = None) -> str:
        """The SQL of *jpql* (see :func:`transpile_jpql`)."""
        return transpile_jpql(jpql, entity, dialect).sql
