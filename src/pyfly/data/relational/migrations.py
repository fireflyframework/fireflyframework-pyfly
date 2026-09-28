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
"""Alembic migrations of the primary datasource: applied when the context starts (Spring Boot's Flyway-style
auto-migrate), and the environment ``pyfly db`` runs them in.

**Startup migrations.** With ``pyfly.data.relational.migrations.enabled=true`` the :class:`MigrationRunner`
bean applies ``alembic upgrade <revision>`` before any other lifecycle bean starts (:data:`MIGRATION_PHASE`;
the schema strategy of ``ddl-auto`` runs right after it). It reuses the project's Alembic environment
(``alembic.ini`` and the ``env.py`` of ``pyfly db init``) and changes nothing else in the process:

- the application's logging stays as it is: the runner tells ``env.py`` not to load ``alembic.ini``'s logging
  configuration (``configure_logger=False``), and hides the file name from an ``env.py`` that would anyway;
- the migrations run on a connection of the application's own engine, handed to ``env.py`` through
  ``config.attributes["connection"]`` (Alembic's connection sharing), so they get the application's URL,
  connect arguments and SQLite setup. An ``env.py`` that does not take a connection (one generated before
  this release) runs in a worker thread on the application's URL, escaped for Alembic's configuration parser
  (a percent-encoded password no longer aborts the start);
- instances that start together run the migrations one at a time
  (:func:`~pyfly.data.relational.schema.schema_lock`).

**The environment of** ``pyfly db``. The ``env.py`` ``pyfly db init`` generates calls the helpers of this
module, so ``pyfly db`` and the startup migrations see the same database and the same models:

- :func:`migration_config` is the application's configuration (``pyfly.yaml``, the active profiles, the
  environment), loaded from the project directory;
- :func:`migration_metadata` imports the application's entity modules (``pyfly.data.relational.migrations.models``)
  and returns ``[Base.metadata, framework_metadata]``: autogenerate compares the database with every model
  and with the framework's own tables, and never proposes dropping either;
- :func:`migration_connection` opens the application's primary datasource (or ``alembic.ini``'s
  ``sqlalchemy.url`` when the configuration has none) under the schema lock, in the transaction the
  migrations run in (:func:`migration_transaction`);
- :func:`refuse_destructive_autogenerate` stops an autogenerate that sees no model while the database has
  tables, instead of writing a revision that drops them.

On SQLite a migration runs with foreign keys off, as Alembic's batch mode needs (a table rebuild would
otherwise cascade or fail), and ``PRAGMA foreign_key_check`` must find nothing before it commits. A batch
rebuild that would silently lose an unnamed ``CHECK`` constraint (Alembic cannot carry one over) fails
instead; the naming convention of ``Base`` names every constraint, so tables it created keep theirs.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import pkgutil
import re
import sys
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import MetaData
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

    from pyfly.core.config import Config

__all__ = [
    "MIGRATION_PHASE",
    "PYFLY_CONFIG_ATTRIBUTE",
    "URL_ATTRIBUTE",
    "MigrationError",
    "MigrationRunner",
    "TargetMetadata",
    "import_models",
    "migration_config",
    "migration_connection",
    "migration_metadata",
    "migration_transaction",
    "migration_url",
    "refuse_destructive_autogenerate",
    "shares_connection",
]

logger = logging.getLogger(__name__)

MIGRATION_PHASE = -(1 << 20) - 1
"""The lifecycle phase of :class:`MigrationRunner`: before every other lifecycle bean, the schema strategy of
``ddl-auto`` (:data:`~pyfly.data.relational.schema.SCHEMA_PHASE`) included."""

PYFLY_CONFIG_ATTRIBUTE = "pyfly_config"
"""``config.attributes`` key: the application's :class:`~pyfly.core.config.Config` (the runner passes it)."""

URL_ATTRIBUTE = "sqlalchemy.url"
"""``config.attributes`` key: the database URL, unescaped (the runner passes it to an ``env.py`` it runs in a
worker thread)."""

_INIT_PLACEHOLDER_URL = "driver://user:pass@localhost/dbname"
"""The ``sqlalchemy.url`` ``alembic init`` writes: not a database."""

_SHARED_CONNECTION = re.compile(r"""attributes\s*(?:\.get\(\s*|\[\s*)["']connection["']""")


class MigrationError(RuntimeError):
    """A migration could not run safely (no database, no models, a constraint a rebuild would lose)."""


# ---------------------------------------------------------------------------------------------------------
# Startup migrations
# ---------------------------------------------------------------------------------------------------------


class MigrationRunner:
    """Startup lifecycle adapter that applies the Alembic migrations once on ``start()`` (see the module
    documentation).

    *engine* is the application's primary engine: the migrations run on one of its connections, under the
    schema lock (waiting at most *lock_timeout* seconds for another instance). Without an engine they run on
    *url* (or ``alembic.ini``'s URL). *config* is the application's configuration, handed to ``env.py``.
    """

    #: Before every other lifecycle bean: the schema the others use is migrated first.
    phase = MIGRATION_PHASE

    def __init__(
        self,
        *,
        url: str = "",
        config_path: str = "alembic.ini",
        revision: str = "head",
        engine: AsyncEngine | None = None,
        config: Config | None = None,
        lock_timeout: float = 300.0,
    ) -> None:
        self._url = url
        self._config_path = config_path
        self._revision = revision
        self._engine = engine
        self._config = config
        self._lock_timeout = lock_timeout

    @property
    def config_path(self) -> str:
        """The Alembic configuration file (``alembic.ini``)."""
        return self._config_path

    @property
    def revision(self) -> str:
        """The revision ``start()`` upgrades to (``head``)."""
        return self._revision

    async def start(self) -> None:
        """Apply the migrations; skip them, with a WARNING, when the Alembic environment is missing."""
        if not os.path.exists(self._config_path):
            logger.warning(
                "pyfly.data.relational.migrations.enabled is true but %s was not found — "
                "run 'pyfly db init' to create the Alembic environment; skipping migrations.",
                self._config_path,
            )
            return
        await self.migrate()
        logger.info("Database migrations applied (alembic upgrade %s)", self._revision)

    async def stop(self) -> None:
        return None

    async def migrate(self) -> None:
        """``alembic upgrade <revision>`` on the application's database (see the module documentation)."""
        cfg = self._alembic_config()
        if self._engine is None:
            await self._upgrade_in_thread(cfg)
            return
        from pyfly.data.relational.schema import schema_lock

        async with self._engine.connect() as connection, schema_lock(connection, timeout=self._lock_timeout):
            if shares_connection(cfg):
                async with migration_transaction(connection):
                    await connection.run_sync(_upgrade, cfg, self._revision)
            else:
                await self._upgrade_in_thread(cfg)

    def _alembic_config(self) -> AlembicConfig:
        from alembic.config import Config as AlembicConfig

        cfg = AlembicConfig(self._config_path)
        # Read the file now (Alembic keeps the parsed options), then hide its name from an env.py that does not
        # honor configure_logger: one that calls logging.config.fileConfig(config.config_file_name) when there
        # is one would otherwise replace the application's logging (disable every existing logger, drop
        # PyFly's handlers, set root to WARNING). An env.py that honors it keeps the name for its own paths.
        cfg.get_main_option("script_location")
        if not _honors_configure_logger(cfg):
            cfg.config_file_name = None
        cfg.attributes["configure_logger"] = False
        if self._config is not None:
            cfg.attributes[PYFLY_CONFIG_ATTRIBUTE] = self._config
        return cfg

    async def _upgrade_in_thread(self, cfg: AlembicConfig) -> None:
        """Run an ``env.py`` that opens its own connection (and its own event loop) in a worker thread."""
        from alembic import command

        url = self._url or (self._engine.url.render_as_string(hide_password=False) if self._engine is not None else "")
        if url:
            cfg.attributes[URL_ATTRIBUTE] = url
            # Alembic's configuration parser interpolates '%': a percent-encoded password (p%40ss) would abort.
            cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        await asyncio.to_thread(command.upgrade, cfg, self._revision)


def _honors_configure_logger(cfg: AlembicConfig) -> bool:
    """Whether the ``env.py`` of *cfg* reads the ``configure_logger`` attribute before it loads a logging
    configuration (the ``env.py`` of ``pyfly db init`` since 26.09.08 does, as Alembic's cookbook advises)."""
    from alembic.script import ScriptDirectory

    try:
        env_py = Path(ScriptDirectory.from_config(cfg).env_py_location)
        return "configure_logger" in env_py.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — unreadable: hide the name, as from an env.py that does not honor it
        return False


def _upgrade(connection: Connection, cfg: AlembicConfig, revision: str) -> None:
    from alembic import command

    cfg.attributes["connection"] = connection
    try:
        command.upgrade(cfg, revision)
    finally:
        cfg.attributes.pop("connection", None)


def shares_connection(cfg: AlembicConfig) -> bool:
    """Whether the environment's ``env.py`` runs on a connection handed to it in
    ``config.attributes["connection"]`` (Alembic's connection sharing; the ``env.py`` of ``pyfly db init``
    does)."""
    from alembic.script import ScriptDirectory

    try:
        source = Path(ScriptDirectory.from_config(cfg).env_py_location).read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(_SHARED_CONNECTION.search(source))


# ---------------------------------------------------------------------------------------------------------
# The transaction a migration runs in
# ---------------------------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def migration_transaction(connection: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """The transaction the migrations run in on *connection*, committed when the block succeeds.

    Alembic runs inside it and leaves the commit to it (the connection is already in a transaction when
    ``env.py`` configures the context). On SQLite it starts with ``BEGIN IMMEDIATE``, runs with foreign keys
    off (Alembic's batch mode rebuilds a table by dropping it, which enforced keys would cascade or refuse),
    refuses a batch rebuild that would lose an unnamed ``CHECK`` constraint, and checks every foreign key
    (``PRAGMA foreign_key_check``) before it commits.
    """
    from sqlalchemy import event

    from pyfly.data.relational.schema import schema_transaction

    sqlite = connection.dialect.name == "sqlite"
    enforced = await connection.run_sync(_sqlite_foreign_keys, False) if sqlite else False
    try:
        async with schema_transaction(connection):
            sync_connection = connection.sync_connection
            if sqlite and sync_connection is not None:
                event.listen(sync_connection, "before_cursor_execute", _refuse_check_loss)
            try:
                yield connection
            finally:
                if sqlite and sync_connection is not None:
                    event.remove(sync_connection, "before_cursor_execute", _refuse_check_loss)
            if sqlite and enforced:
                violations: list[Any] = list((await connection.exec_driver_sql("PRAGMA foreign_key_check")).all())
                if violations:
                    raise MigrationError(
                        "The migration leaves rows whose foreign keys point nowhere (PRAGMA foreign_key_check): "
                        + ", ".join(f"{row[0]} rowid {row[1]} -> {row[2]}" for row in violations[:10])
                        + ". Nothing was committed."
                    )
    finally:
        if sqlite and enforced:
            await connection.run_sync(_sqlite_foreign_keys, True)


def _sqlite_foreign_keys(connection: Connection, enforce: bool) -> bool:
    """Set ``PRAGMA foreign_keys`` on *connection*'s driver connection, outside any transaction (the pragma is
    a no-op inside one), and return whether keys were enforced before."""
    cursor = connection.connection.dbapi_connection.cursor()  # type: ignore[union-attr]
    try:
        cursor.execute("PRAGMA foreign_keys")
        row = cursor.fetchone()
        before = bool(row[0]) if row else False
        cursor.execute(f"PRAGMA foreign_keys={'ON' if enforce else 'OFF'}")
    finally:
        cursor.close()
    return before


_DROP_TABLE = re.compile(r'^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:"([^"]+)"|`([^`]+)`|(\w+))\s*;?\s*$', re.IGNORECASE)
_CHECK = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
_CONSTRAINT_NAME = re.compile(r"CONSTRAINT\s+(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\w+)\s*$", re.IGNORECASE)


def _refuse_check_loss(
    conn: Connection, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    """``before_cursor_execute`` listener: refuse the ``DROP TABLE`` of an Alembic batch rebuild on SQLite when
    the rebuilt table (``_alembic_tmp_<name>``) lacks an unnamed ``CHECK`` of the table it replaces."""
    match = _DROP_TABLE.match(statement)
    if match is None:
        return
    name = next(group for group in match.groups() if group)
    original = _table_sql(conn, name)
    rebuilt = _table_sql(conn, f"_alembic_tmp_{name}")
    if original is None or rebuilt is None:
        return
    kept = {_compact(expression) for _named, expression in _checks(rebuilt)}
    lost = [expression for named, expression in _checks(original) if not named and _compact(expression) not in kept]
    if lost:
        raise MigrationError(
            f"A batch migration of table {name!r} would drop its unnamed CHECK constraint(s) "
            + ", ".join(f"CHECK ({expression})" for expression in lost)
            + ": Alembic cannot carry an unnamed CHECK over when SQLite rebuilds a table. Name the constraint "
            "(CheckConstraint(..., name=...); Base's naming convention names new ones), recreate the table with "
            "the named constraint in a migration of its own, then run this one. Nothing was changed."
        )


def _table_sql(conn: Connection, name: str) -> str | None:
    cursor = conn.connection.dbapi_connection.cursor()  # type: ignore[union-attr]
    try:
        cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
        row = cursor.fetchone()
    finally:
        cursor.close()
    return str(row[0]) if row and row[0] else None


def _compact(expression: str) -> str:
    """*expression* without whitespace: the same rule declared again with other spacing (``x>0``, ``x > 0``)."""
    return "".join(expression.split())


def _checks(create_table: str) -> list[tuple[bool, str]]:
    """The ``CHECK`` constraints of a ``CREATE TABLE`` statement: whether each is named, and its expression
    (whitespace and case normalized)."""
    found: list[tuple[bool, str]] = []
    for match in _CHECK.finditer(create_table):
        depth, start = 1, match.end()
        index = start
        while index < len(create_table) and depth:
            if create_table[index] == "(":
                depth += 1
            elif create_table[index] == ")":
                depth -= 1
            index += 1
        expression = " ".join(create_table[start : index - 1].split()).lower()
        named = bool(_CONSTRAINT_NAME.search(create_table[: match.start()]))
        found.append((named, expression))
    return found


# ---------------------------------------------------------------------------------------------------------
# The environment of pyfly db (env.py)
# ---------------------------------------------------------------------------------------------------------


def _project_dir(alembic_config: AlembicConfig) -> Path:
    name = alembic_config.config_file_name
    return Path(name).resolve().parent if name else Path.cwd()


def migration_config(alembic_config: AlembicConfig) -> Config:
    """The application's configuration: the one the runner handed over, or ``pyfly.yaml`` (with the active
    profiles and the environment) from the directory of ``alembic.ini``."""
    handed = alembic_config.attributes.get(PYFLY_CONFIG_ATTRIBUTE)
    if handed is not None:
        return handed  # type: ignore[no-any-return]
    from pyfly.context.environment import Environment
    from pyfly.core.config import Config

    directory = _project_dir(alembic_config)
    base = Config.from_sources(directory)
    profiles = Environment(base).active_profiles
    loaded = Config.from_sources(directory, active_profiles=profiles) if profiles else base
    alembic_config.attributes[PYFLY_CONFIG_ATTRIBUTE] = loaded
    return loaded


def import_models(modules: Iterable[str], *, skip: Iterable[str] = (), search_path: Path | None = None) -> None:
    """Import *modules*, and every module under the ones that are packages, so their entities are declared on
    ``Base.metadata``. Modules named in *skip* (and ``__main__`` modules) are left out, with every module under
    them: a skipped package is never imported. When a module's top-level package cannot be imported,
    *search_path*'s ``src`` directory is put on ``sys.path`` and the import retried (a src-layout project run
    without installing it).

    A module that fails to import raises :class:`MigrationError`: autogenerate without its models would
    propose dropping their tables.
    """
    skipped = set(skip)
    for name in modules:
        module = _import_first(name, search_path)
        path = getattr(module, "__path__", None)
        if path is not None:
            _import_package(name, path, skipped)


def _import_package(name: str, path: Iterable[str], skipped: set[str]) -> None:
    """Import the modules of package *name*, and of its subpackages, never entering a skipped one (walking
    into a package imports it)."""
    for info in pkgutil.iter_modules(path, prefix=f"{name}."):
        if info.name in skipped or info.name.rsplit(".", 1)[-1] == "__main__":
            continue
        module = _import(info.name)
        subpath = getattr(module, "__path__", None) if info.ispkg else None
        if subpath is not None:
            _import_package(info.name, subpath, skipped)


def _import_first(name: str, search_path: Path | None) -> Any:
    """Import *name*; when its top-level package is not importable, retry with *search_path*'s ``src``
    directory on ``sys.path`` (only then: an installed or running application keeps its path)."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        source = (search_path / "src").resolve() if search_path is not None else None
        if source is None or error.name != name.split(".", 1)[0] or not source.is_dir() or str(source) in sys.path:
            raise _import_error(name, error) from error
        sys.path.insert(0, str(source))
    except Exception as error:
        raise _import_error(name, error) from error
    return _import(name)


def _import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception as error:
        raise _import_error(name, error) from error


def _import_error(name: str, error: Exception) -> MigrationError:
    return MigrationError(
        f"Cannot import {name!r} for the migrations ({type(error).__name__}: {error}). List the modules that "
        "declare your entities in pyfly.data.relational.migrations.models."
    )


class TargetMetadata(list["MetaData"]):
    """The ``target_metadata`` of the ``env.py`` of ``pyfly db init``: the application's ``MetaData`` first,
    then the framework's.

    Autogenerate compares the database with every one of them. Alembic operations name the unnamed
    constraints of a revision with the ``naming_convention`` of ``target_metadata``: this list answers with
    its first ``MetaData``'s (``Base.metadata``), and setting it sets that one's, so a history that opts in
    with :func:`~pyfly.data.relational.sqlalchemy.naming.apply_convention_to_operations` (with no argument, or
    ``target_metadata``) keeps its names.
    """

    @property
    def naming_convention(self) -> Any:
        """The naming convention of the application's ``MetaData`` (the first)."""
        return self[0].naming_convention if self else {}

    @naming_convention.setter
    def naming_convention(self, convention: Any) -> None:
        if not self:
            raise ValueError("target_metadata holds no MetaData to give the naming convention to")
        self[0].naming_convention = convention


def migration_metadata(alembic_config: AlembicConfig, *, default_models: Sequence[str] = ()) -> TargetMetadata:
    """``[Base.metadata, framework_metadata]`` (a :class:`TargetMetadata`), after importing the application's
    entity modules.

    The modules are ``pyfly.data.relational.migrations.models`` from the application's configuration, or
    *default_models* (``pyfly db init`` writes the project's package there). A package is imported with every
    module under it, except the application's entry point (``pyfly.app.module``).
    """
    from pyfly.config.properties.data import PREFIX, _string_list
    from pyfly.data.relational.framework_schema import framework_metadata
    from pyfly.data.relational.sqlalchemy.entity import Base

    config = migration_config(alembic_config)
    models = _string_list(config.get(f"{PREFIX}.migrations.models"), f"{PREFIX}.migrations.models") or list(
        default_models
    )
    entry_point = str(config.get("pyfly.app.module") or "").split(":", 1)[0]
    import_models(models, skip=[entry_point] if entry_point else [], search_path=_project_dir(alembic_config))
    return TargetMetadata([Base.metadata, framework_metadata])


def migration_url(alembic_config: AlembicConfig) -> str:
    """The database the migrations run on: the URL the runner handed over, else the application's primary
    datasource (``pyfly.data.relational.url``), else ``alembic.ini``'s ``sqlalchemy.url``."""
    handed = alembic_config.attributes.get(URL_ATTRIBUTE)
    if handed:
        return str(handed)
    from pyfly.config.properties.data import RelationalProperties

    url = RelationalProperties.from_config(migration_config(alembic_config)).url
    if url:
        return url
    configured = alembic_config.get_main_option("sqlalchemy.url")
    if configured and configured != _INIT_PLACEHOLDER_URL:
        return configured
    raise MigrationError(
        "No database to migrate: set pyfly.data.relational.url in pyfly.yaml (or the environment), or "
        "sqlalchemy.url in alembic.ini."
    )


@contextlib.asynccontextmanager
async def migration_connection(alembic_config: AlembicConfig) -> AsyncIterator[AsyncConnection]:
    """A connection to the database of :func:`migration_url`, inside :func:`migration_transaction`, under the
    schema lock.

    The application's primary datasource is built as the application builds it (pool, connect arguments,
    SQLite setup) and closed afterwards; a URL from ``alembic.ini`` gets a plain engine.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from pyfly.config.properties.data import RelationalProperties
    from pyfly.data.relational.schema import schema_lock

    engine: AsyncEngine
    registry = None
    url = migration_url(alembic_config)
    config = migration_config(alembic_config)
    properties = RelationalProperties.from_config(config)
    if alembic_config.attributes.get(URL_ATTRIBUTE) is None and properties.url == url:
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        registry = DataSourceRegistry(config)
        engine = registry.primary.engine
    else:
        engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with (
            engine.connect() as connection,
            schema_lock(connection, timeout=properties.schema.lock_timeout),
            migration_transaction(connection),
        ):
            yield connection
    finally:
        if registry is not None:
            await registry.close()
        else:
            await engine.dispose()


def refuse_destructive_autogenerate(context: MigrationContext, revision: Any, directives: list[Any]) -> None:
    """``process_revision_directives`` hook: stop an autogenerate that sees no entity model and would drop the
    database's tables.

    Without the models (``env.py`` imported none), autogenerate proposes dropping every table of the
    database, and a startup migration would apply the revision. It raises :class:`MigrationError` instead,
    before the revision is written. The framework's tables are models too (``framework_metadata``), so a
    database that holds only those passes.
    """
    from alembic.operations import ops

    from pyfly.data.relational.framework_schema import framework_metadata

    target = context.opts.get("target_metadata")
    metadatas = list(target) if isinstance(target, (list, tuple)) else [target]
    if any(metadata.tables for metadata in metadatas if metadata is not None and metadata is not framework_metadata):
        return
    dropped = [
        operation.table_name
        for script in directives
        for upgrade in getattr(script, "upgrade_ops_list", ())
        for operation in upgrade.ops
        if isinstance(operation, ops.DropTableOp)
    ]
    if dropped:
        directives[:] = []
        raise MigrationError(
            "Autogenerate found no entity model, and the revision would drop the tables "
            + ", ".join(sorted(dropped)[:10])
            + (" and more" if len(dropped) > 10 else "")
            + ". List the modules that declare your entities in pyfly.data.relational.migrations.models (or "
            "import them in alembic/env.py). No revision was written."
        )
