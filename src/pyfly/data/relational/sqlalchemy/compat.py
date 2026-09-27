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
"""What differs between the SQLAlchemy lines PyFly supports, in one place.

PyFly requires SQLAlchemy 2.0.50 or later and is tested on the 2.0 and 2.1 lines (CI runs the suite and
mypy on both). Where the two lines behave differently in a way the framework depends on, the difference is
bridged here, by feature detection rather than by version number:

- **DISTINCT ON.** 2.1 adds ``select(...).ext(postgresql.distinct_on(...))``, a syntax extension held outside
  the ``_distinct_on`` expressions that ``select(...).distinct(*expressions)`` fills (that spelling is
  deprecated on 2.1). :func:`drop_distinct_on_extension` takes the extension off a statement; on 2.0 there is
  none to take.
- **Python types of column types.** 2.0 raises ``NotImplementedError`` for a type whose Python type it does not
  know, where 2.1 answers ``object``; and 2.1's ``JSON`` no longer answers ``dict``. :func:`python_type`
  answers as 2.0 did, on both.
- **Foreign key targets.** 2.1 names the target of a ``ForeignKey`` by tokens (``target_tokens``), which hold a
  table name that contains a dot, and its dotted ``target_fullname`` raises for such a name; 2.0 has only the
  dotted form. :func:`foreign_key_target_table` reads the table name from whichever the line has.
- **Typing.** 2.0 types a ``SELECT`` of one integer column ``Select[tuple[int]]`` and 2.1 ``Select[int]`` (rows
  are generic over their column types, PEP 646). The statement helpers annotate such statements
  ``Select[Any]``, which reads the same on both lines.

The private ``Select`` and ``Result`` attributes the statement helpers read are the same on both lines; they
are pinned by ``tests/data/test_statements.py::TestSqlAlchemyInternals``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, Select
from sqlalchemy.dialects.postgresql import ext as _postgresql_ext
from sqlalchemy.types import TypeEngine

# Looked up rather than imported, so that the module type-checks against either line (CI runs mypy on both).
_DISTINCT_ON_EXTENSION: type[Any] | None = getattr(_postgresql_ext, "DistinctOnClause", None)  # None on 2.0

__all__ = ["drop_distinct_on_extension", "foreign_key_target_table", "python_type"]


def drop_distinct_on_extension(statement: Select[Any]) -> None:
    """Take a ``DISTINCT ON`` given as a syntax extension (``ext(postgresql.distinct_on(...))``, SQLAlchemy 2.1)
    off *statement*, in place; any other extension at the same point of the statement stays.

    Resetting ``_distinct`` does not take it off: the dialect would still render its ``ON (...)``, after a
    ``SELECT`` that is no longer ``DISTINCT``. Call it on a statement of your own (a generative copy).
    """
    if _DISTINCT_ON_EXTENSION is None:
        return
    extended: Any = statement  # the extension point is 2.1's alone
    extensions = extended._pre_columns_clause
    if extensions is None:
        return
    elements = getattr(extensions, "clauses", (extensions,))  # several extensions come as an ElementList
    kept = [element for element in elements if not isinstance(element, _DISTINCT_ON_EXTENSION)]
    if len(kept) == len(elements):
        return
    if kept:
        extended.apply_syntax_extension_point(lambda _existing: kept, "pre_columns")
    else:
        extended._pre_columns_clause = None


def python_type(column_type: TypeEngine[Any]) -> type[Any]:
    """The Python type of *column_type*'s values, as SQLAlchemy 2.0 reports it, on 2.0 and 2.1 alike.

    A ``JSON`` column holds a ``dict`` (2.1's ``JSON`` names no type: any JSON value fits the column). A type
    SQLAlchemy knows no Python type for raises ``NotImplementedError`` (2.1 answers ``object`` instead).
    """
    if isinstance(column_type, JSON):
        return dict
    answered: type[Any] = column_type.python_type
    if answered is object:
        raise NotImplementedError(f"{column_type!r} has no Python type")
    return answered


def foreign_key_target_table(foreign_key: ForeignKey) -> str:
    """The name of the table *foreign_key* refers to, which need not exist yet.

    Read from ``target_tokens`` on 2.1, so a table name with a dot in it is read whole (``target_fullname``
    raises for it there); from the dotted ``target_fullname`` on 2.0.
    """
    tokens = getattr(foreign_key, "target_tokens", None)
    if tokens is not None:
        return str(tokens.table_name)
    return str(foreign_key.target_fullname).split(".")[-2]
