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
"""Adopt the constraint naming convention on a database created before it: the one-time rename migration.

``Base.metadata`` names every unnamed constraint with
:data:`~pyfly.data.relational.sqlalchemy.entity.NAMING_CONVENTION` since 26.09.08. A database created before
keeps the names its backend chose (``accounts_email_key`` on PostgreSQL, ``email`` on MySQL, none on SQLite),
and a revision written against the convention names (``drop_constraint("uq_accounts_email")``) fails there.
Opt in once, from an Alembic revision, and the existing constraints get the convention names::

    # migrations/versions/xxxx_adopt_the_constraint_naming_convention.py
    from alembic import op

    import myapp.models  # noqa: F401 — the models whose tables are renamed
    from pyfly.data.relational.sqlalchemy import Base
    from pyfly.data.relational.sqlalchemy.naming import rename_constraints_to_convention


    def upgrade() -> None:
        rename_constraints_to_convention(op, Base.metadata)

:func:`rename_constraints_to_convention` compares every table of the metadata that exists in the database
with its model, matches each constraint by what it constrains (a unique constraint by its columns, a foreign
key by its columns and referred table, a check by its SQL text, the primary key), and renames the ones whose
name differs:

- **PostgreSQL**: ``ALTER TABLE ... RENAME CONSTRAINT``, for unique, foreign key, check and primary key
  constraints;
- **MySQL/MariaDB**: ``RENAME INDEX`` for a unique key; a foreign key or a check is dropped and created again
  under its new name, from the model's definition (the primary key is always ``PRIMARY``);
- **SQLite**: each table with a constraint to rename is recreated (Alembic's batch mode) with the convention,
  which names its unnamed constraints.

It is idempotent: a second run renames nothing. Other dialects raise ``NotImplementedError``. A check whose SQL
text the backend rewrote beyond whitespace, quoting and parentheses is not matched and keeps its name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, MetaData, Table, UniqueConstraint, inspect
from sqlalchemy.engine import Inspector

PRIMARY_KEY = "primary_key"
UNIQUE = "unique"
FOREIGN_KEY = "foreign_key"
CHECK = "check"

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
_SUPPORTED = frozenset({"postgresql", "sqlite"}) | _MYSQL_FAMILY


@dataclass(frozen=True)
class ConstraintRename:
    """A constraint :func:`rename_constraints_to_convention` renamed: its table, its kind (``primary_key``,
    ``unique``, ``foreign_key``, ``check``), the name the backend gave it (``None``: it had none) and the
    convention's."""

    table: str
    kind: str
    old: str | None
    new: str


def rename_constraints_to_convention(
    operations: Any, metadata: MetaData | None = None, *, tables: Iterable[str] | None = None
) -> list[ConstraintRename]:
    """Rename the constraints of the database behind *operations* (Alembic's ``op``, or an
    ``alembic.operations.Operations``) to the names *metadata* gives them (default: ``Base.metadata``).

    *tables* restricts the tables considered (default: every table of *metadata* that exists). Returns what
    was renamed. See the module documentation.
    """
    if metadata is None:
        from pyfly.data.relational.sqlalchemy.entity import Base

        metadata = Base.metadata
    bind = operations.get_bind()
    dialect = bind.dialect.name
    if dialect not in _SUPPORTED:
        raise NotImplementedError(
            f"rename_constraints_to_convention supports PostgreSQL, MySQL, MariaDB and SQLite, not {dialect}: "
            "rename the constraints with the backend's own statements"
        )
    inspector = inspect(bind)
    existing = set(inspector.get_table_names())
    selected = None if tables is None else set(tables)
    renames: list[ConstraintRename] = []
    for table in metadata.sorted_tables:
        if table.name not in existing or (selected is not None and table.name not in selected):
            continue
        planned = _plan(inspector, table, dialect)
        if not planned:
            continue
        if dialect == "sqlite":
            with operations.batch_alter_table(
                table.name, recreate="always", naming_convention=metadata.naming_convention
            ):
                pass
        else:
            for rename, constraint in planned:
                _apply(operations, bind, dialect, table, rename, constraint)
        renames.extend(rename for rename, _constraint in planned)
    return renames


def _plan(inspector: Inspector, table: Table, dialect: str) -> list[tuple[ConstraintRename, Any]]:
    planned: list[tuple[ConstraintRename, Any]] = []

    def rename(kind: str, old: Any, constraint: Any) -> None:
        new = constraint.name
        if isinstance(new, str) and new and old != new:
            planned.append((ConstraintRename(table.name, kind, old if old else None, new), constraint))

    if dialect not in _MYSQL_FAMILY and table.primary_key.columns:
        rename(PRIMARY_KEY, inspector.get_pk_constraint(table.name).get("name"), table.primary_key)

    reflected_unique = inspector.get_unique_constraints(table.name)
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            columns = {column.name for column in constraint.columns}
            found = next((u for u in reflected_unique if set(u["column_names"]) == columns), None)
            if found is not None:
                rename(UNIQUE, found["name"], constraint)

    reflected_keys = inspector.get_foreign_keys(table.name)
    for foreign_key in table.foreign_key_constraints:
        local = [element.parent.name for element in foreign_key.elements]
        referred = foreign_key.elements[0].column.table.name
        match = next(
            (
                fk
                for fk in reflected_keys
                if list(fk["constrained_columns"]) == local and fk["referred_table"] == referred
            ),
            None,
        )
        if match is not None:
            rename(FOREIGN_KEY, match["name"], foreign_key)

    reflected_checks = list(inspector.get_check_constraints(table.name))
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint):
            text = _normalized(str(constraint.sqltext))
            check = next((ck for ck in reflected_checks if _normalized(str(ck["sqltext"])) == text), None)
            if check is not None:
                reflected_checks.remove(check)
                rename(CHECK, check["name"], constraint)
    return planned


def _apply(operations: Any, bind: Any, dialect: str, table: Table, rename: ConstraintRename, constraint: Any) -> None:
    quote = bind.dialect.identifier_preparer.quote
    if dialect == "postgresql":
        operations.execute(
            f"ALTER TABLE {quote(table.name)} RENAME CONSTRAINT {quote(rename.old)} TO {quote(rename.new)}"
        )
        return
    # MySQL and MariaDB
    if rename.kind == UNIQUE:
        operations.execute(f"ALTER TABLE {quote(table.name)} RENAME INDEX {quote(rename.old)} TO {quote(rename.new)}")
    elif rename.kind == FOREIGN_KEY:
        assert isinstance(constraint, ForeignKeyConstraint)
        operations.drop_constraint(rename.old, table.name, type_="foreignkey")
        operations.create_foreign_key(
            rename.new,
            table.name,
            constraint.elements[0].column.table.name,
            [element.parent.name for element in constraint.elements],
            [element.column.name for element in constraint.elements],
            onupdate=constraint.onupdate,
            ondelete=constraint.ondelete,
        )
    elif rename.kind == CHECK:
        assert isinstance(constraint, CheckConstraint)
        operations.drop_constraint(rename.old, table.name, type_="check")
        operations.create_check_constraint(rename.new, table.name, constraint.sqltext)


def _normalized(sql: str) -> str:
    """The SQL text of a check without the whitespace, quoting and parentheses a backend adds to it."""
    return re.sub(r"[\s`\"()]", "", sql).lower()
