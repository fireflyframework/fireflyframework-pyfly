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

**Results** follow the method's return annotation, never the SQL's text (an annotation that does not resolve at
runtime, a name imported only for type checking, is logged at WARNING and read as absent; the others still
count):

- the entity (``list[User]``, ``User | None``): the query runs as ``select(User).from_statement(text(sql))``,
  so its rows are the unit of work's own entities (identity-mapped: a change to one is flushed with the unit,
  ``save()`` updates it), typed and mapped by attribute through the entity's columns, their relationships
  loaded as any read loads them (one loaded from the statement itself, ``lazy="joined"`` or
  ``lazy="subquery"``, by one more statement instead, since a text statement can be neither joined nor nested),
  and the unit's pending changes are flushed before it runs;
- a scalar (``int``, ``bool``, ``str``, ``list[str]``...), a row (``tuple``), a row by column name (``dict``),
  a :func:`~pyfly.data.projection.projection` or any other class built from the row's columns by name: the
  statement runs as it is, after the unit's pending changes are flushed;
- one result (``X | None``) is ``None`` when no row matches and raises
  :class:`~pyfly.data.query_parser.IncorrectResultSizeException` when more than one does;
- an unannotated method returns the entities, or the value of a query that starts with ``SELECT COUNT`` or
  ``SELECT EXISTS``.

**Statements that change rows** (``UPDATE``, ``DELETE``, ``INSERT``, after a ``WITH`` clause too) need
:func:`~pyfly.data.query.modifying` and return the number of rows they changed (``-> int`` or ``-> int | None``;
``None`` for ``-> None``); without it the repository fails to build. The statement's verb decides the unit a call
outside a transaction runs in: a read unit for a ``SELECT`` (or ``VALUES``) that changes nothing, a write unit for
any other statement (a ``@modifying`` one, a ``CALL``, which may return ``None``, a ``SELECT`` whose ``WITH``
clause deletes). A statement returning ``None`` has its result closed unread. MySQL refuses an ``UPDATE`` or a
``DELETE`` whose subquery reads its own table (error 1093; MariaDB accepts it), and MariaDB a ``WITH`` clause
before one.

**Arguments** bind by name (``:name``, a parameter of the method) and, in JPQL, by position (``?1`` is the first
parameter after ``self``); the method takes them by position or by keyword. ``IN (:ids)`` (or ``IN :ids``) binds
a collection (a list, a tuple, a set, a generator...), one value per element, and any other value as a list of
one: a string, bytes, a mapping, a number or ``None`` is one value, never iterated, so ``IN (:code)`` with ``"AB"``
matches ``AB`` (not ``A`` and ``B``), and with ``None`` matches no row, as ``IN (NULL)`` does. An empty collection
matches no row, and after ``NOT IN`` every row, whatever the column's type: SQLAlchemy writes an empty list as a set
of integers, so for that call ``IN`` is written ``= ANY('{}')`` (``NOT IN``: ``<> ALL('{}')``) on PostgreSQL, which
types the array as the column, and ``IN (SELECT NULL FROM DUAL WHERE 1 = 0)`` on MySQL and MariaDB, whose ``NULL``
compares with any type (a MariaDB ``UUID``, too). A ``UUID`` binds as SQLAlchemy's ``Uuid`` and an aware
``datetime`` as ``UtcDateTime`` (a collection by its elements), as entity columns of those types store them; other
values bind as the driver takes them. A parameter the query never uses is accepted, and binds nothing.

**JPQL** (``native=False``, the default) is rewritten token by token, for the dialect the query runs on:

1. ``FROM Entity alias`` (and ``JOIN``, ``UPDATE``) names the entity's table (quoted where the dialect needs it);
   the alias stays, so correlated subqueries keep their correlation. Aliases are scoped as SQL scopes them: a
   subquery that declares an alias again names its own entity with it;
2. ``SELECT alias`` selects the entity's columns (``alias.col1, alias.col2, ...``); ``COUNT(alias)`` is
   ``COUNT(*)``;
3. ``alias.attribute`` names the attribute's column (``alias.column``); a name that is neither an attribute nor a
   column of the entity fails when the repository is built;
4. the literals ``true`` and ``false`` are what the dialect accepts (``true``/``false``, or ``1``/``0`` where
   booleans are integers); ``IS TRUE`` and ``IS FALSE`` are left as written;
5. the target of an ``UPDATE`` or a ``DELETE`` drops its alias (MySQL before 8.0.16 accepts none), and a
   reference to it names the table (the ``SET`` targets stay bare, as PostgreSQL requires); every other alias
   stays, the one of a subquery over the target's own entity included, so that subquery stays correlated with
   the row being changed.

String literals, quoted identifiers and comments are never rewritten; they are read as the dialect reads them
(a backslash escapes a quote on MySQL and MariaDB; PostgreSQL's ``E'...'`` and ``$$...$$`` are literals;
``[name]`` is a quoted identifier on SQL Server and SQLite only, and elsewhere an array's bracket, so
``ARRAY[:a, :b]`` and ``tags[:i]`` bind their parameters on PostgreSQL). A name that is not an entity (a table in
a subquery, ``schema.table``) is left as it is. In JPQL and native SQL alike, the colons of literals, quoted
identifiers and comments are escaped, so ``'10:30'``, ``'a :b'`` or ``-- see :x`` never become parameters; a
colon already escaped the way ``text()`` documents (``'10\\:30'``) is escaped once, not twice. The startup check
reads brackets as arrays (it runs before the dialect is known): a name with a colon in it is quoted with double
quotes (``"at:noon"``, or MySQL's backticks), whose colons are never parameters.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, TypeVar

from sqlalchemy import Uuid, bindparam, column, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ColumnProperty, Mapper, selectinload
from sqlalchemy.types import NullType

from pyfly.data.post_processor import resolved_annotations
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

_logger = logging.getLogger(__name__)

__all__ = ["CompiledQuery", "QueryExecutor", "TranspiledQuery", "query", "tokenize", "transpile_jpql"]


# ---------------------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------------------


def _token_pattern(string: str, quoted: str, *, brackets: bool) -> re.Pattern[str]:
    """The token pattern whose string literals are *string* and double-quoted names *quoted*; ``[name]`` is a
    quoted name too when *brackets* (and otherwise a ``[`` and a ``]`` of their own)."""
    bracketed = r"|\[[^\]]*\]" if brackets else ""
    return re.compile(
        rf"""
          (?P<space>\s+)
        | (?P<comment>--[^\n]*|/\*.*?\*/)
        | (?P<escape_string>[Ee]'(?:[^'\\]|\\.|'')*')
        | (?P<dollar_string>\$(?P<tag>(?:[A-Za-z_][A-Za-z0-9_]*)?)\$.*?\$(?P=tag)\$)
        | (?P<string>{string})
        | (?P<quoted>{quoted}|`(?:[^`]|``)*`{bracketed})
        | (?P<cast>::)
        | (?P<bind>:[A-Za-z_][A-Za-z0-9_]*)
        | (?P<positional>\?[0-9]+)
        | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
        | (?P<number>[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)
        | (?P<other>.)
        """,
        re.VERBOSE | re.DOTALL,
    )


_STANDARD_LITERALS = (r"'(?:[^']|'')*'", r'"(?:[^"]|"")*"')
"""String literals as the SQL standard writes them: a quote inside one is doubled (PostgreSQL, SQLite...)."""

_BACKSLASH_LITERALS = (r"'(?:[^'\\]|\\.|'')*'", r'"(?:[^"\\]|\\.|"")*"')
"""String literals as MySQL and MariaDB read them: a backslash escapes the character after it, a quote too."""

_TOKEN_PATTERNS = {
    (backslash, brackets): _token_pattern(
        *(_BACKSLASH_LITERALS if backslash else _STANDARD_LITERALS), brackets=brackets
    )
    for backslash in (False, True)
    for brackets in (False, True)
}
"""The token patterns by (backslash-escaped literals, bracket-quoted names)."""

_BRACKET_QUOTING = frozenset({"mssql", "sqlite"})
"""The dialects that quote a name with brackets (``[order]``). Elsewhere a bracket builds or subscripts an array
(PostgreSQL's ``ARRAY[:a, :b]`` and ``tags[:i]``), and the parameters inside it are parameters."""

_STRING_KINDS = {"escape_string": "string", "dollar_string": "string"}
"""PostgreSQL's ``E'...'`` and ``$tag$...$tag$`` literals are string tokens like the others."""

_OPAQUE = frozenset({"string", "quoted", "comment"})
"""The tokens whose text is never rewritten (their colons are escaped, so ``text()`` binds none of them)."""

_UNESCAPED_COLON = re.compile(r"(?<!\\):")

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
_READS = frozenset({"SELECT", "VALUES", "TABLE"})
_VERBS = _DML | _READS
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


def tokenize(sql: str, dialect: Dialect | None = None) -> list[Token]:
    """The tokens of *sql* (their texts concatenate back to it), its string literals read as *dialect* reads
    them: MySQL and MariaDB escape a quote inside one with a backslash (``'it\\'s'``), the others only by
    doubling it (``'it''s'``). A query that reads only the other way is read that way (a MySQL server in
    ``NO_BACKSLASH_ESCAPES`` mode; a query checked before its dialect is known, ``dialect=None``). PostgreSQL's
    ``E'...'`` and dollar-quoted (``$$...$$``, ``$tag$...$tag$``) literals are strings on every dialect.
    ``[name]`` is a quoted name on SQL Server and SQLite only: on the other dialects, and before the dialect is
    known, a bracket builds or subscripts an array (``ARRAY[:a, :b]``), whose parameters are parameters.

    An unterminated string literal or quoted identifier raises
    :class:`~pyfly.data.query_parser.InvalidQueryMethodError`.
    """
    backslash = dialect is not None and dialect.name in ("mysql", "mariadb")
    brackets = dialect is not None and dialect.name in _BRACKET_QUOTING
    try:
        return _tokens(sql, _TOKEN_PATTERNS[backslash, brackets])
    except InvalidQueryMethodError as error:
        try:
            return _tokens(sql, _TOKEN_PATTERNS[not backslash, brackets])
        except InvalidQueryMethodError:
            raise error from None


def _tokens(sql: str, pattern: re.Pattern[str]) -> list[Token]:
    tokens: list[Token] = []
    for match in pattern.finditer(sql):
        kind = match.lastgroup or "other"
        value = match.group()
        if kind == "other" and value in ("'", '"', "`"):
            raise InvalidQueryMethodError(f"Unterminated {value} in the query: {sql}")
        tokens.append(Token(_STRING_KINDS.get(kind, kind), value))
    return tokens


def _significant(tokens: Sequence[Token]) -> list[int]:
    return [index for index, token in enumerate(tokens) if token.kind not in ("space", "comment")]


def _verb(tokens: Sequence[Token], significant: Sequence[int]) -> int | None:
    """The index of the statement's verb: its first keyword past leading parentheses (``SELECT``, ``UPDATE``...),
    or, after a ``WITH`` clause, the keyword at the ``WITH``'s depth that starts the statement it introduces."""
    depth = level = 0
    first: int | None = None
    for index in significant:
        token = tokens[index]
        if first is None:
            if token.text == "(":
                depth += 1
                continue
            if token.kind != "word":
                return None
            if token.upper != "WITH":
                return index
            first, level = index, depth
            continue
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        elif depth == level and token.kind == "word" and token.upper in _VERBS:
            return index
    return first


def _writes(tokens: Sequence[Token], significant: Sequence[int], kind: str) -> bool:
    """Whether the statement may change rows: any statement but a read (``SELECT``, ``VALUES``, ``TABLE``), and
    a read whose ``WITH`` clause changes rows (PostgreSQL's ``WITH gone AS (DELETE ... RETURNING id) SELECT``)."""
    if kind not in _READS:
        return True
    for position in range(1, len(significant) - 1):
        token = tokens[significant[position]]
        if (
            token.kind == "word"
            and token.upper in _DML
            and tokens[significant[position - 1]].text == "("
            and tokens[significant[position + 1]].kind == "word"  # DELETE FROM, not the REPLACE(...) function
        ):
            return True
    return False


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


_EMPTY_LISTS: dict[str, tuple[str, str]] = {
    "postgresql": ("= ANY('{}')", "<> ALL('{}')"),
    "mysql": ("IN (SELECT NULL FROM DUAL WHERE 1 = 0)", "NOT IN (SELECT NULL FROM DUAL WHERE 1 = 0)"),
}
"""Dialect -> how ``IN :name`` and ``NOT IN :name`` are written for an empty list (:func:`_without_empty_lists`);
MariaDB's dialect is ``mysql`` too."""


def _without_empty_lists(described: TranspiledQuery, dialect: Dialect, names: frozenset[str]) -> TranspiledQuery:
    """*described* for a call that binds no value to its ``IN`` lists *names*, on a dialect of
    :data:`_EMPTY_LISTS`.

    SQLAlchemy writes an empty list as a set of integers (``IN (SELECT CAST(NULL AS INTEGER) WHERE 1!=1)`` on
    PostgreSQL, ``IN (SELECT _in_0 FROM (SELECT 1 AS _in_0) ...)`` on MySQL and MariaDB), which PostgreSQL refuses
    to compare with a column of another type, and MariaDB with a ``UUID`` or ``INET6`` one. So ``IN :name`` is
    written ``= ANY('{}')`` and ``NOT IN :name`` ``<> ALL('{}')`` on PostgreSQL, which types the empty array as the
    column it is compared with, and ``[NOT] IN (SELECT NULL FROM DUAL WHERE 1 = 0)`` on MySQL and MariaDB, whose
    ``NULL`` compares with any type. They answer as an empty list does (``IN``: false; ``NOT IN``: true, for a
    ``NULL`` too). A name the query also binds outside an ``IN`` list stays bound there."""
    in_empty, not_in_empty = _EMPTY_LISTS[dialect.name]
    tokens = tokenize(described.sql, dialect)
    significant = _significant(tokens)
    for position, index in enumerate(significant):
        token = tokens[index]
        if token.kind != "bind" or token.text[1:] not in names or position == 0:
            continue
        operator = significant[position - 1]
        if tokens[operator].upper != "IN":
            continue
        negated = position >= 2 and tokens[significant[position - 2]].upper == "NOT"
        start = significant[position - 2] if negated else operator
        for between in range(start, index + 1):
            tokens[between] = Token("space", "")
        tokens[start] = Token("other", not_in_empty if negated else in_empty)
    binds = tuple(_binds(tokens))
    return replace(
        described,
        sql="".join(token.text for token in tokens),
        binds=binds,
        expanding=described.expanding & frozenset(binds),
    )


def _escaped_colons(tokens: Sequence[Token]) -> None:
    """Escape the colons of string literals, quoted names and comments in place, so ``text()`` never reads one
    as a parameter. A colon the query already escapes (``\\:``, the escape ``text()`` documents) stays escaped
    once: ``text()`` turns each ``\\:`` back into ``:``."""
    for token in tokens:
        if token.kind in _OPAQUE and ":" in token.text:
            token.text = _UNESCAPED_COLON.sub(r"\\:", token.text)


# ---------------------------------------------------------------------------------------------------------
# JPQL
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TranspiledQuery:
    """A query ready for ``text()``: its SQL for one dialect, and what binding it needs."""

    sql: str
    kind: str
    """The statement's verb (``SELECT``, ``UPDATE``, ``DELETE``, ``INSERT``, ``CALL``...): its first keyword, or
    after a ``WITH`` clause the keyword of the statement the clause introduces."""
    binds: tuple[str, ...]
    """The named parameters it binds, in order of appearance (with repeats)."""
    expanding: frozenset[str]
    """The parameters that bind a collection (``IN :name``)."""
    columns: tuple[tuple[str, Any], ...] = ()
    """The result columns' labels and SQL types, when the select list names entity attributes only."""
    writes: bool = False
    """Whether the statement may change rows: any statement but a ``SELECT`` (or ``VALUES``, ``TABLE``), and a
    ``SELECT`` whose ``WITH`` clause changes rows."""

    @property
    def is_dml(self) -> bool:
        """Whether the statement changes rows and counts them (``UPDATE``, ``DELETE``, ``INSERT``...)."""
        return self.kind in _DML


class _Jpql:
    """The token rewrite of one JPQL query for one dialect (module documentation).

    An alias is scoped as SQL scopes it: a reference names the alias the innermost enclosing query declares, so a
    subquery that declares the target's alias again names its own entity with it. Only the target of an
    ``UPDATE`` or a ``DELETE`` loses its alias; every other alias stays, the target's own table in a subquery
    included, which keeps the subquery correlated with the row being changed.
    """

    def __init__(self, jpql: str, entity: type, dialect: Dialect | None, positional: Sequence[str]) -> None:
        self._jpql = jpql
        self._entity = entity
        self._dialect = dialect
        self._positional = positional
        self._tokens = tokenize(jpql, dialect)
        self._significant = _significant(self._tokens)
        self._positions = {index: position for position, index in enumerate(self._significant)}
        self._scopes = self._enclosing()
        self._mappers = _entities_of(entity)
        self._aliases: dict[str, Mapper[Any]] = {}
        self._tables: dict[int, Mapper[Any]] = {}
        self._alias_tokens: set[int] = set()
        self._declared: dict[str, set[int]] = {}
        """Alias -> the scopes (:meth:`_scope`) that declare it."""
        self._declared_by: dict[int, tuple[str, list[int]]] = {}
        """Entity token -> its alias and the tokens that declare it (``AS`` included)."""
        self._target: int | None = None
        self._target_alias: str | None = None
        self._dropped: set[int] = set()
        self._verb = _verb(self._tokens, self._significant)
        self.kind = self._tokens[self._verb].upper if self._verb is not None else ""

    def transpile(self) -> TranspiledQuery:
        writes = _writes(self._tokens, self._significant, self.kind)
        self._declarations()
        self._dml_target()
        columns = self._typed_columns()
        self._rewrite()
        _escaped_colons(self._tokens)
        expanding = _expanding(self._tokens)
        return TranspiledQuery(
            sql="".join(token.text for token in self._tokens),
            kind=self.kind,
            binds=tuple(_binds(self._tokens)),
            expanding=frozenset(expanding),
            columns=columns,
            writes=writes,
        )

    # -- helpers ------------------------------------------------------------------------------------------

    def _fail(self, message: str) -> InvalidQueryMethodError:
        return InvalidQueryMethodError(f"{message} (in the query {self._jpql!r})")

    def _next(self, index: int | None) -> int | None:
        """The index of the significant token after *index*, or ``None``."""
        if index is None:
            return None
        position = self._positions[index] + 1
        return self._significant[position] if position < len(self._significant) else None

    def _previous(self, index: int | None) -> int | None:
        if index is None:
            return None
        position = self._positions[index] - 1
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

    def _enclosing(self) -> dict[int, tuple[int, ...]]:
        """Each significant token's enclosing parentheses (the indexes of their ``(``), outermost first."""
        scopes: dict[int, tuple[int, ...]] = {}
        stack: list[int] = []
        for index in self._significant:
            text_ = self._tokens[index].text
            if text_ == ")" and stack:
                stack.pop()
            scopes[index] = tuple(stack)
            if text_ == "(":
                stack.append(index)
        return scopes

    def _scope(self, index: int) -> int:
        """The query a token belongs to: its innermost enclosing ``(``, or ``-1`` for the statement itself."""
        enclosing = self._scopes[index]
        return enclosing[-1] if enclosing else -1

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
            declaring: list[int] = []
            alias = self._next(index)
            if alias is not None and self._upper(alias) == "AS":
                declaring.append(alias)
                self._alias_tokens.add(alias)
                alias = self._next(alias)
            if alias is None or self._tokens[alias].kind != "word" or self._upper(alias) in _CLAUSE_KEYWORDS:
                continue
            name = self._tokens[alias].text.lower()
            known = self._aliases.get(name)
            if known is not None and known is not mapper:
                raise self._fail(f"the alias {self._tokens[alias].text!r} names two entities")
            self._aliases[name] = mapper
            self._alias_tokens.add(alias)
            declaring.append(alias)
            self._declared.setdefault(name, set()).add(self._scope(index))
            self._declared_by[index] = (name, declaring)

    def _dml_target(self) -> None:
        """Find the entity an ``UPDATE`` or a ``DELETE`` changes (right after ``UPDATE``, or ``DELETE FROM``), and
        drop its alias: the one alias the statement loses."""
        if self.kind not in ("UPDATE", "DELETE"):
            return
        target = self._next(self._verb)
        if self.kind == "DELETE" and self._upper(target) == "FROM":
            target = self._next(target)
        if target is None or target not in self._tables:
            return  # a table that is not an entity keeps its alias, as written
        self._target = target
        declared = self._declared_by.get(target)
        if declared is not None:
            self._target_alias, declaring = declared
            self._dropped.update(declaring)

    def _names_target(self, index: int, alias: str) -> bool:
        """Whether *alias* at *index* names the target of the ``UPDATE`` or ``DELETE``: the innermost query around
        it that declares the alias is the statement itself."""
        if self._target is None or alias != self._target_alias:
            return False
        declared = self._declared.get(alias, set())
        for scope in reversed((-1, *self._scopes[index])):
            if scope in declared:
                return scope == self._scope(self._target)
        return False

    # -- the select list's types --------------------------------------------------------------------------

    def _typed_columns(self) -> tuple[tuple[str, Any], ...]:
        """The labels and types of the outermost select list when it names entity attributes only
        (``a.name``, ``a.name AS label``), so SQLite returns them typed too; ``()`` otherwise."""
        if self.kind != "SELECT" or self._verb is None:
            return ()
        items: list[list[int]] = [[]]
        depth = 0
        for index in self._significant[self._positions[self._verb] + 1 :]:  # after SELECT
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
        in_set = False
        depth = 0
        for index in self._significant:
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
            if index in self._dropped:
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
            alias = token.text.lower()
            if token.kind == "word" and alias in self._aliases and index not in self._alias_tokens:
                self._reference(index, token, target=self._names_target(index, alias), in_set=in_set and depth == 0)

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
        negated = self._upper(before) == "NOT"
        if self._upper(before) == "IS" or (negated and self._upper(self._previous(before)) == "IS"):
            return  # IS TRUE, IS NOT FALSE: a predicate of its own
        if self._dialect is not None and not self._dialect.supports_native_boolean:
            token.text = "1" if _BOOLEANS[token.text.lower()] else "0"

    def _reference(self, index: int, token: Token, *, target: bool, in_set: bool) -> None:
        """Rewrite ``alias.attribute`` to ``alias.column``; the target of an ``UPDATE`` or a ``DELETE``, which has
        no alias, is named by its table instead (bare as a ``SET`` target, as PostgreSQL requires)."""
        mapper = self._aliases[token.text.lower()]
        dot = self._next(index)
        if dot is not None and self._text(dot) == ".":
            attribute = self._next(dot)
            if attribute is None:
                raise self._fail(f"{token.text}. names nothing")
            if self._text(attribute) == "*":
                return
            name = self._column(mapper, self._tokens[attribute].text)
            self._tokens[attribute].text = self._quote(name)
            if target:
                if in_set and self._text(self._next(attribute)) == "=":
                    token.text, self._tokens[dot].text = "", ""  # SET col = ...: the target stays bare
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
        if counted and self._upper(self._previous(before)) == "COUNT":
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


def _entities_of(entity: type) -> dict[str, Mapper[Any]]:
    """The mapped classes a JPQL query of *entity* may name, by class name: the entity's own name is the entity,
    and a name several classes share is the one declared in the entity's module (a name that is still ambiguous
    is left out, and so stays a table name)."""
    mapper: Mapper[Any] = sa_inspect(entity)
    candidates: dict[str, list[Mapper[Any]]] = {}
    for other in mapper.registry.mappers:
        candidates.setdefault(other.class_.__name__, []).append(other)
    mappers: dict[str, Mapper[Any]] = {}
    for name, found in candidates.items():
        if len(found) > 1:
            found = [other for other in found if other.class_.__module__ == entity.__module__]
        if len(found) == 1:
            mappers[name] = found[0]
    mappers[entity.__name__] = mapper
    return mappers


def transpile_jpql(
    jpql: str, entity: type, dialect: Dialect | None = None, *, positional: Sequence[str] = ()
) -> TranspiledQuery:
    """The SQL of *jpql* for *dialect* (module documentation); ``?N`` names ``positional[N - 1]``. Without a
    dialect, names are not quoted and boolean literals stay ``true``/``false``."""
    return _Jpql(jpql, entity, dialect, positional).transpile()


def _native(sql: str, dialect: Dialect | None = None) -> TranspiledQuery:
    """A native query as *dialect* reads it: its colons in literals, quoted names and comments escaped, and its
    ``IN (:name)`` lists expanding; nothing else is rewritten."""
    tokens = tokenize(sql, dialect)
    significant = _significant(tokens)
    verb = _verb(tokens, significant)
    kind = tokens[verb].upper if verb is not None else ""
    writes = _writes(tokens, significant, kind)
    _escaped_colons(tokens)
    expanding = _expanding(tokens)
    return TranspiledQuery(
        sql="".join(token.text for token in tokens),
        kind=kind,
        binds=tuple(_binds(tokens)),
        expanding=frozenset(expanding),
        writes=writes,
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
        self._by_dialect: dict[tuple[str, frozenset[str]], tuple[TranspiledQuery, Any]] = {}
        self._lock = threading.Lock()
        described = self._transpile(None)  # checks the query once, at startup, whatever the dialect
        self._kind = described.kind
        self._writes = described.writes
        self._check(described)
        self._shape = self._shape_of(described, return_type)

    # -- compilation ------------------------------------------------------------------------------------

    @property
    def is_modifying(self) -> bool:
        """Whether the statement changes rows (``@modifying``)."""
        return self._modifying is not None

    @property
    def reads(self) -> bool:
        """Whether the statement only reads, so it may run in a read unit: a ``SELECT`` (or ``VALUES``) whose
        ``WITH`` clause changes nothing. Any other statement, a ``CALL`` included, runs in a write unit."""
        return not self._writes

    @property
    def shape(self) -> ResultShape:
        """The shape of the result (from the return annotation)."""
        return self._shape

    def _fail(self, message: str) -> InvalidQueryMethodError:
        return InvalidQueryMethodError(f"{self._name}: {message}")

    def _transpile(self, dialect: Dialect | None) -> TranspiledQuery:
        try:
            if self._native:
                return _native(self._query, dialect)
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
            if return_type is type(None):
                return ResultShape(ResultKind.NONE, ElementKind.SCALAR, None)
            if return_type in (None, int, Any) or _is_int(return_type):
                return ResultShape(ResultKind.ONE, ElementKind.SCALAR, int)
            raise self._fail(f"a @modifying query returns int (the row count) or None, not {return_type}")
        if return_type is None or return_type is Any:
            legacy = _LEGACY_SCALAR.match(described.sql)
            if legacy is not None:
                scalar = int if legacy.group(1).upper() == "COUNT" else bool
                return ResultShape(ResultKind.ONE, ElementKind.SCALAR, scalar)
            return ResultShape(ResultKind.LIST, ElementKind.ENTITY, self._entity)
        try:
            shape = result_shape(return_type, self._entity)
        except InvalidQueryMethodError as error:
            raise self._fail(str(error)) from None
        if shape.kind in (ResultKind.PAGE, ResultKind.SLICE):
            raise self._fail("a @query method returns a list or one result, not a Page or Slice; page a derived query")
        if shape.kind is ResultKind.NONE and not described.writes:
            raise self._fail("a query that is not @modifying returns what it selects, not None")
        if shape.element is ElementKind.OBJECT and _is_mapped(shape.type):
            return ResultShape(shape.kind, ElementKind.ENTITY, shape.type)
        return shape

    def _prepared(self, dialect: Dialect, empty: frozenset[str] = frozenset()) -> tuple[TranspiledQuery, Any]:
        """The transpiled query and its ``text()`` clause for *dialect*, built on first use; with *empty*, for a
        call that binds no value to those ``IN`` lists (:func:`_without_empty_lists`)."""
        key = (dialect.name, empty)
        prepared = self._by_dialect.get(key)
        if prepared is None:
            described = self._transpile(dialect)
            if empty:
                described = _without_empty_lists(described, dialect, empty)
            clause: Any = text(described.sql)
            if described.columns and self._shape.element is not ElementKind.ENTITY:
                clause = clause.columns(*(column(label, type_) for label, type_ in described.columns))
            with self._lock:
                prepared = self._by_dialect.setdefault(key, (described, clause))
        return prepared

    # -- execution ----------------------------------------------------------------------------------------

    async def __call__(self, session: AsyncSession, **arguments: Any) -> Any:
        dialect = session.sync_session.get_bind().dialect
        described, clause = self._prepared(dialect)
        values: dict[str, Any] = {}
        for name in dict.fromkeys(described.binds):
            if name not in arguments:
                raise TypeError(f"{self._name}: the query needs a value for :{name}")
            values[name] = _listed(arguments[name]) if name in described.expanding else arguments[name]
        if dialect.name in _EMPTY_LISTS:
            empty = frozenset(name for name in described.expanding if not values[name])
            if empty:  # SQLAlchemy's empty list is a set of integers there (_without_empty_lists)
                described, clause = self._prepared(dialect, empty)
                values = {name: values[name] for name in dict.fromkeys(described.binds)}
        bound = _bound(clause, values, described.expanding) if values else clause
        if self._modifying is not None:
            return await self._modify(session, bound, dialect)
        if self._shape.kind is ResultKind.NONE:  # a statement run for what it does (a CALL): nothing to read
            await _flush_pending(session)
            (await session.execute(bound)).close()  # rows it returns are not read, nor left open
            return None
        if self._shape.element is ElementKind.ENTITY:
            entity = self._shape.type or self._entity
            statement = select(entity).from_statement(bound).options(*_eager_as_selectin(entity))
            return self._shaped(unique_entities(await session.execute(statement)))
        await _flush_pending(session)
        result = await session.execute(bound)
        return self._shaped(self._rows(result))

    async def _modify(self, session: AsyncSession, statement: Any, dialect: Dialect) -> Any:
        options = self._modifying or ModifyingOptions()
        if options.flush_automatically:
            await _flush_pending(session)
        result = await session.execute(statement)
        rowcount = getattr(result, "rowcount", None)
        result.close()  # an UPDATE ... RETURNING's rows are not read, nor left open
        count = int(rowcount) if rowcount is not None else -1
        if count < 0 and dialect.name == "sqlite":
            # Python's sqlite3 counts the rows of a statement that starts with INSERT, UPDATE, DELETE or REPLACE
            # only: SQLite itself knows how many rows a WITH ... UPDATE changed.
            count = int((await session.execute(text("SELECT changes()"))).scalar_one())
        count = max(count, 0)
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


_ONE_VALUE: tuple[type, ...] = (str, bytes, bytearray, memoryview, Mapping)
"""Iterables that are one value in an ``IN`` list, never iterated: a string, binary data, a mapping."""


def _listed(value: Any) -> list[Any]:
    """The values an ``IN`` list binds for *value*: a collection's elements (any iterable: a list, a tuple, a set,
    a generator...), and any other value, a string, bytes, a mapping or ``None`` included, as a list of one. A
    string iterated letter by letter matched ``A`` and ``B`` for ``"AB"``; ``None`` binds ``IN (NULL)``, which
    matches no row."""
    if type(value) is list:
        return value
    return list(value) if isinstance(value, Iterable) and not isinstance(value, _ONE_VALUE) else [value]


def _parameter(name: str, value: Any, expanding: bool) -> Any:
    """The bind of *value* for ``:name`` (:func:`_bound`). After ``IN`` (*expanding*) it binds a list
    (:func:`_listed`), as SQLAlchemy indexes an expanding value, typed by its first value that is not ``None``."""
    if expanding:
        value = _listed(value)
        sample = next((item for item in value if item is not None), None)
    else:
        sample = value
    if isinstance(sample, uuid.UUID):
        return bindparam(name, value, type_=Uuid(), expanding=expanding)
    if isinstance(sample, datetime) and sample.tzinfo is not None:
        return bindparam(name, value, type_=UtcDateTime(), expanding=expanding)
    return bindparam(name, value, type_=NullType(), expanding=expanding)


async def _flush_pending(session: AsyncSession) -> None:
    """Flush the session's pending changes before a statement the ORM does not flush for (a raw one)."""
    if session.sync_session.autoflush and (session.new or session.deleted or session.dirty):
        await session.flush()


_EAGER_FROM_THE_STATEMENT: tuple[Any, ...] = ("joined", False, "subquery")
"""The loading strategies that load a relationship from the statement itself: with a join, or with a subquery of
it (``lazy="subquery"``)."""


def _eager_as_selectin(entity: type) -> list[Any]:
    """``selectin`` loads of the relationships *entity*'s mapping loads from the statement itself (``lazy="joined"``
    or ``lazy="subquery"``): a text statement can be neither joined nor nested, so each is loaded with one more
    statement instead, and is there on the results as a read of the entity has it."""
    mapper: Mapper[Any] = sa_inspect(entity)
    return [
        selectinload(getattr(entity, relationship.key))
        for relationship in mapper.relationships
        if relationship.lazy in _EAGER_FROM_THE_STATEMENT
    ]


def _is_int(annotation: Any) -> bool:
    """Whether *annotation* is ``int``, or ``int | None``: a row count."""
    try:
        shape = result_shape(annotation)
    except InvalidQueryMethodError:
        return False
    return shape.kind is ResultKind.ONE and shape.type is int


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
    ) -> CompiledQuery:
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
        hints, unresolved = resolved_annotations(function, {entity.__name__: entity})
        if unresolved:
            # A name imported only for type checking: that annotation is read as absent (an unresolved return
            # annotation runs the query as an unannotated one, as before its result followed the annotation), the
            # others still count, and the log says which did not resolve and why.
            _logger.warning(
                "query_method_annotations_unresolved",
                extra={
                    "method": described,
                    "annotations": ", ".join(unresolved),
                    "error": "; ".join(f"{type(error).__name__}: {error}" for error in unresolved.values()),
                },
            )
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
