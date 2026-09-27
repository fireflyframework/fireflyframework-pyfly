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
"""Persistence exception translation: backend exceptions become the kernel's.

Spring translates persistence exceptions at the ``@Repository`` boundary and at commit, so services
handle one backend-neutral hierarchy. PyFly does the same:

- every repository call (``Repository``, ``SoftDeleteRepository``, subclasses, derived and ``@query``
  methods) raises the translated exception;
- the unit-of-work boundary (``@transactional``, ``TransactionTemplate``, auto units, a ``NESTED``
  savepoint release) translates what its commit, or the flush its commit runs, raises;
- the web layer's converters use the same translation for an exception that reached it untranslated.

The translations:

========================================  =====================================================
Backend exception                         Kernel exception (HTTP 409)
========================================  =====================================================
unique or primary key violation           :class:`~pyfly.kernel.exceptions.DuplicateKeyException`
other integrity violation (foreign key,   :class:`~pyfly.kernel.exceptions.DataIntegrityException`
not null, check, exclusion)
``StaleDataError`` (optimistic locking)   :class:`~pyfly.kernel.exceptions.OptimisticLockingFailureException`
========================================  =====================================================

The translated exception is raised *from* the backend's (``__cause__``). Its message and context name the
kind of violation and the violated constraint (``uq_users_email``: the same name on every backend, thanks
to :data:`~pyfly.data.relational.sqlalchemy.entity.NAMING_CONVENTION`), and never carry the SQL statement or
its bound values, which may be personal data; the full driver message is logged at ``DEBUG`` by this
module's logger. MariaDB's "record has changed since last read" (its snapshot isolation catching a stale
write) is an optimistic-locking failure too. Other exceptions (a connection failure, a deadlock, a
serialization failure) are left as they are, so retry rules that name them keep working.

A backend adds its translations with :func:`register_exception_translator` (the document backend
registers MongoDB's). This module imports no backend library at import time.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Protocol, runtime_checkable

from pyfly.kernel.exceptions import (
    DataIntegrityException,
    DuplicateKeyException,
    OptimisticLockingFailureException,
    PyFlyException,
)

logger = logging.getLogger(__name__)

INTEGRITY_ERROR_CODE = "INTEGRITY_ERROR"
"""The ``code`` of every translated integrity violation (the one the web layer always answered 409 with)."""

OPTIMISTIC_LOCKING_FAILURE_CODE = "OPTIMISTIC_LOCKING_FAILURE"
"""The ``code`` of a translated optimistic-locking failure."""


@runtime_checkable
class PersistenceExceptionTranslator(Protocol):
    """Translates one backend's exceptions (Spring's ``PersistenceExceptionTranslator``)."""

    def translate_exception_if_possible(self, error: BaseException) -> PyFlyException | None:
        """The kernel exception for *error*, or ``None`` when this translator does not know it."""
        ...


# ---------------------------------------------------------------------------------------------------------
# Integrity violations, described without SQL or values
# ---------------------------------------------------------------------------------------------------------

UNIQUE = "unique"
FOREIGN_KEY = "foreign_key"
NOT_NULL = "not_null"
CHECK = "check"
EXCLUSION = "exclusion"

_MESSAGES = {
    UNIQUE: "Duplicate key: unique constraint {name} violated",
    FOREIGN_KEY: "Foreign key constraint {name} violated",
    NOT_NULL: "A required value is missing: not-null constraint violated",
    CHECK: "Check constraint {name} violated",
    EXCLUSION: "Exclusion constraint {name} violated",
}
_ANONYMOUS_MESSAGES = {
    UNIQUE: "Duplicate key: a unique constraint was violated",
    FOREIGN_KEY: "A foreign key constraint was violated",
    CHECK: "A check constraint was violated",
    EXCLUSION: "An exclusion constraint was violated",
}
_GENERIC_MESSAGE = "Data integrity constraint violated"


def integrity_violation(violation: str | None, constraint: str | None) -> DataIntegrityException:
    """The kernel exception for an integrity violation of kind *violation* on *constraint* (either may be
    unknown): :class:`DuplicateKeyException` for a unique key, :class:`DataIntegrityException` otherwise."""
    if violation is None:
        message = _GENERIC_MESSAGE
    elif constraint is not None and "{name}" in _MESSAGES[violation]:
        message = _MESSAGES[violation].format(name=repr(constraint))
    else:
        message = _ANONYMOUS_MESSAGES.get(violation, _MESSAGES[violation])
    context: dict[str, Any] = {}
    if violation is not None:
        context["violation"] = violation
    if constraint is not None:
        context["constraint"] = constraint
    kind = DuplicateKeyException if violation == UNIQUE else DataIntegrityException
    return kind(message, code=INTEGRITY_ERROR_CODE, context=context)


def optimistic_locking_failure() -> OptimisticLockingFailureException:
    """The kernel exception for an optimistic-locking conflict."""
    return OptimisticLockingFailureException(
        "The entity was changed or deleted by another transaction since it was read",
        code=OPTIMISTIC_LOCKING_FAILURE_CODE,
    )


# ---------------------------------------------------------------------------------------------------------
# SQLAlchemy
# ---------------------------------------------------------------------------------------------------------

_POSTGRESQL_STATES = {
    "23505": UNIQUE,
    "23503": FOREIGN_KEY,
    "23502": NOT_NULL,
    "23514": CHECK,
    "23P01": EXCLUSION,
}
_MYSQL_ERRORS = {
    1062: UNIQUE,  # ER_DUP_ENTRY
    1586: UNIQUE,  # ER_DUP_ENTRY_WITH_KEY_NAME
    1451: FOREIGN_KEY,  # ER_ROW_IS_REFERENCED_2
    1452: FOREIGN_KEY,  # ER_NO_REFERENCED_ROW_2
    1216: FOREIGN_KEY,  # ER_NO_REFERENCED_ROW
    1217: FOREIGN_KEY,  # ER_ROW_IS_REFERENCED
    1048: NOT_NULL,  # ER_BAD_NULL_ERROR
    1364: NOT_NULL,  # ER_NO_DEFAULT_FOR_FIELD
    3819: CHECK,  # MySQL: ER_CHECK_CONSTRAINT_VIOLATED
    4025: CHECK,  # MariaDB: ER_CONSTRAINT_FAILED
}
_SQLITE_ERRORS = {
    "SQLITE_CONSTRAINT_UNIQUE": UNIQUE,
    "SQLITE_CONSTRAINT_PRIMARYKEY": UNIQUE,
    "SQLITE_CONSTRAINT_FOREIGNKEY": FOREIGN_KEY,
    "SQLITE_CONSTRAINT_NOTNULL": NOT_NULL,
    "SQLITE_CONSTRAINT_CHECK": CHECK,
}
_SQLITE_MESSAGES = (
    ("UNIQUE constraint failed", UNIQUE),
    ("FOREIGN KEY constraint failed", FOREIGN_KEY),
    ("NOT NULL constraint failed", NOT_NULL),
    ("CHECK constraint failed", CHECK),
)
_MYSQL_RECORD_CHANGED = 1020
"""MariaDB's ``ER_CHECKREAD``: a row changed since the transaction's snapshot read it
(``innodb_snapshot_isolation``)."""
_MYSQL_KEY = re.compile(r"for key '([^']+)'")
_MYSQL_CONSTRAINT = re.compile(r"CONSTRAINT `([^`]+)`")
_MYSQL_CHECK = re.compile(r"[Cc]heck constraint '([^']+)'")
_IDENTIFIER = re.compile(r"^\w+$")


class SqlAlchemyExceptionTranslator:
    """Translates SQLAlchemy's ``IntegrityError`` and ``StaleDataError`` (PostgreSQL, MySQL, MariaDB and SQLite
    drivers; any other dialect's integrity errors get the generic translation)."""

    def translate_exception_if_possible(self, error: BaseException) -> PyFlyException | None:
        try:
            from sqlalchemy.exc import DBAPIError, IntegrityError
            from sqlalchemy.orm.exc import StaleDataError
        except ImportError:  # pragma: no cover — SQLAlchemy is an optional dependency
            return None
        if isinstance(error, StaleDataError):
            return optimistic_locking_failure()
        if isinstance(error, IntegrityError):
            violation, constraint = _describe(error.orig)
            return integrity_violation(violation, constraint)
        if isinstance(error, DBAPIError):
            # MySQL/MariaDB drivers raise some integrity violations (a CHECK) as OperationalError, and
            # MariaDB's snapshot isolation reports a stale write as "Record has changed since last read".
            mysql_error = _mysql_error(error.orig)
            if mysql_error is not None:
                code, message = mysql_error
                if code == _MYSQL_RECORD_CHANGED:
                    return optimistic_locking_failure()
                if code in _MYSQL_ERRORS:
                    return integrity_violation(_MYSQL_ERRORS[code], _mysql_constraint(message))
        return None


def _describe(driver_error: Any) -> tuple[str | None, str | None]:
    """The kind of violation and the constraint name the driver's error reports."""
    state = getattr(driver_error, "sqlstate", None) or getattr(driver_error, "pgcode", None)
    if isinstance(state, str) and state in _POSTGRESQL_STATES:
        return _POSTGRESQL_STATES[state], _postgresql_constraint(driver_error)
    sqlite_name = getattr(driver_error, "sqlite_errorname", None)
    if isinstance(sqlite_name, str) or type(driver_error).__module__ == "sqlite3":
        return _describe_sqlite(driver_error, sqlite_name)
    mysql_error = _mysql_error(driver_error)
    if mysql_error is not None and mysql_error[0] in _MYSQL_ERRORS:
        return _MYSQL_ERRORS[mysql_error[0]], _mysql_constraint(mysql_error[1])
    return None, None


def _mysql_error(driver_error: Any) -> tuple[int, str] | None:
    """The error number and message of a MySQL/MariaDB driver error (asyncmy, aiomysql, PyMySQL: ``args``)."""
    args: tuple[Any, ...] = tuple(getattr(driver_error, "args", ()))
    if len(args) >= 2 and isinstance(args[0], int):
        return args[0], str(args[1])
    return None


def _postgresql_constraint(driver_error: Any) -> str | None:
    # asyncpg (through SQLAlchemy's adaptation: the asyncpg exception is the cause) and psycopg (diag).
    for candidate in (driver_error, getattr(driver_error, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
    diag = getattr(driver_error, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) and name else None


def _mysql_constraint(message: str) -> str | None:
    for pattern in (_MYSQL_KEY, _MYSQL_CONSTRAINT, _MYSQL_CHECK):
        found = pattern.search(message)
        if found is not None:
            # MySQL 8 names a unique key 'table.key'.
            return found.group(1).rsplit(".", 1)[-1]
    return None


def _describe_sqlite(driver_error: Any, error_name: str | None) -> tuple[str | None, str | None]:
    message = str(driver_error)
    violation = _SQLITE_ERRORS.get(error_name or "")
    if violation is None:
        violation = next((kind for prefix, kind in _SQLITE_MESSAGES if message.startswith(prefix)), None)
    detail = message.split(": ", 1)[1] if ": " in message else ""
    if violation == CHECK:
        # A named CHECK reports its name; an unnamed one its SQL text, which is not a name.
        return violation, detail if _IDENTIFIER.match(detail) else None
    if violation == UNIQUE and detail:
        return violation, _unique_constraint_of(detail)
    return violation, None


def _unique_constraint_of(columns: str) -> str | None:
    """The name of the unique constraint (or primary key, or unique index) of the mapped table that SQLite
    reports by its columns (``"accounts.email"``, ``"lines.order_code, lines.line_no"``)."""
    qualified = [column.strip() for column in columns.split(",")]
    tables = {name.rsplit(".", 1)[0] for name in qualified if "." in name}
    if len(tables) != 1:
        return None
    table_name = tables.pop()
    wanted = {name.rsplit(".", 1)[1] for name in qualified}
    try:
        from sqlalchemy import Index, PrimaryKeyConstraint, UniqueConstraint

        from pyfly.data.relational.sqlalchemy.entity import Base
    except ImportError:  # pragma: no cover
        return None
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return None
    keys = (UniqueConstraint, PrimaryKeyConstraint)
    candidates: list[Any] = [constraint for constraint in table.constraints if isinstance(constraint, keys)]
    candidates += [index for index in table.indexes if isinstance(index, Index) and index.unique]
    for candidate in candidates:
        if {column.name for column in candidate.columns} == wanted and isinstance(candidate.name, str):
            return str(candidate.name)
    return None


# ---------------------------------------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_TRANSLATORS: list[PersistenceExceptionTranslator] = [SqlAlchemyExceptionTranslator()]


def register_exception_translator(translator: PersistenceExceptionTranslator) -> None:
    """Add *translator* to the chain, ahead of the built-in ones (idempotent)."""
    with _LOCK:
        if translator not in _TRANSLATORS:
            _TRANSLATORS.insert(0, translator)


def unregister_exception_translator(translator: PersistenceExceptionTranslator) -> None:
    """Remove *translator* from the chain (idempotent)."""
    with _LOCK:
        if translator in _TRANSLATORS:
            _TRANSLATORS.remove(translator)


def translate_exception(error: BaseException) -> BaseException:
    """*error* translated to a kernel persistence exception, chained from it (``__cause__``), or *error*
    itself when no translator knows it (a kernel exception already, a connection failure, anything else)."""
    if isinstance(error, PyFlyException) or not isinstance(error, Exception):
        return error
    for translator in tuple(_TRANSLATORS):
        translated = translator.translate_exception_if_possible(error)
        if translated is not None:
            translated.__cause__ = error  # as ``raise translated from error`` would
            translated.__suppress_context__ = True
            logger.debug(
                "persistence_exception_translated",
                extra={"translated": type(translated).__name__, "context": translated.context},
                exc_info=(type(error), error, error.__traceback__),
            )
            return translated
    return error
