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
"""The datasource registry: every SQLAlchemy engine the application uses, built in one place.

Before this registry each module built its own engine from a URL: the primary, the read replica, every
named datasource, the event store, the snapshot store, the saga persistence and the PostgreSQL cache.
Against one database that was seven pools (up to 105 connections per process); only the primary got the
pool settings, and only the primary was disposed on shutdown.

A :class:`DataSourceRegistry` now owns them all. It builds the primary, its replica and the named
datasources from ``pyfly.data.relational.*`` through one factory, which gives every engine the same
treatment:

- the pool settings, cast from strings where env vars and placeholders deliver them;
- ``pool.recycle`` (1800 s by default) and no pre-ping by default;
- the connect arguments (``connect-args``: asyncpg ``statement_cache_size`` behind pgbouncer,
  ``server_settings``, SSL, timeouts) and a default ``application_name`` on PostgreSQL;
- the SQLite setup of :mod:`~pyfly.data.relational.dialect_customizers` (foreign keys, WAL, the
  ``BEGIN`` recipe), with ``StaticPool`` kept for ``:memory:``;
- a ``do_connect`` hook that takes the credentials from the live configuration (or from a
  :class:`~pyfly.data.relational.dialect_customizers.DataSourceCredentialsProvider`) for every new
  connection, so a rotated password needs no restart.

Modules name a datasource and never build an engine: :meth:`DataSourceRegistry.resolve` returns the
primary when a module configures no URL of its own, the registered datasource whose URL is identical,
or registers an extra named datasource that gets the same treatment. :meth:`DataSourceRegistry.close`
disposes every engine exactly once.

There is one registry per :class:`~pyfly.core.config.Config` (one per application context):
:meth:`DataSourceRegistry.for_config` returns it, whichever auto-configuration asks first, and the
``datasource_registry`` bean is that same object. A closed registry is forgotten, so a restarted
context builds a fresh one.

Usage::

    registry = ctx.get_bean(DataSourceRegistry)
    primary = registry.primary                    # DataSource
    reporting = registry.get("reporting")
    async with reporting.sessionmaker() as session:
        ...
    registry.primary.capabilities.fast_autocommit_reads   # True only on PostgreSQL
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import MetaData, event
from sqlalchemy.engine import URL, Dialect, make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, ConnectionPoolEntry, PoolProxiedConnection, QueuePool

from pyfly.config.properties.data import PREFIX, PRIMARY, DataSourceProperties, RelationalProperties
from pyfly.container.ordering import get_order
from pyfly.core.config import Config
from pyfly.data.relational.dialect_customizers import (
    AfterBeginCustomizer,
    DataSourceCredentialsProvider,
    begin_execution_options,
    install_credentials_hook,
    install_sqlite_customizer,
    is_file_database,
    run_after_begin,
)

__all__ = [
    "PRIMARY",
    "DataSource",
    "DataSourceCapabilities",
    "DataSourceConfigurationError",
    "DataSourceRegistry",
    "MeteredAsyncQueuePool",
    "NoSuchDataSourceError",
    "datasource_of",
]

_logger = logging.getLogger(__name__)

DEV_FALLBACK_URL = "sqlite+aiosqlite:///./app.db"
"""The database an application in the ``dev`` profile gets when it configures no URL at all."""

_SAVEPOINT_DIALECTS = frozenset({"sqlite", "postgresql", "mysql", "mariadb", "mssql", "oracle"})

# Largest IN list / bound-parameter count per statement the driver accepts (SQLite before 3.32: 999).
_MAX_IN_PARAMS = {"postgresql": 32767, "mysql": 65535, "mariadb": 65535, "mssql": 2000, "oracle": 1000}

_CREDENTIALS_EPOCH = "pyfly_credentials_epoch"
"""Pool-entry ``info`` key: the credential epoch of the datasource when the connection was opened."""

_STATIC_ISOLATION_LEVELS = {
    "sqlite": frozenset({"READ UNCOMMITTED", "SERIALIZABLE"}),
    "postgresql": frozenset({"READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"}),
    "mysql": frozenset({"READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"}),
    "mariadb": frozenset({"READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"}),
}

# Datasources by engine and by session factory, so a legacy holder of either (a service's
# ``_session_factory``) maps back to its datasource. Weak: a disposed and dropped engine leaves.
_BY_ENGINE: weakref.WeakKeyDictionary[AsyncEngine, DataSource] = weakref.WeakKeyDictionary()
_BY_SESSIONMAKER: weakref.WeakKeyDictionary[async_sessionmaker[AsyncSession], DataSource] = weakref.WeakKeyDictionary()


class DataSourceConfigurationError(ValueError):
    """A datasource cannot be built from the configuration (most often: no URL)."""


class NoSuchDataSourceError(KeyError):
    """No datasource is registered under the requested name."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "No such datasource"


def datasource_of(target: AsyncEngine | async_sessionmaker[AsyncSession]) -> DataSource | None:
    """The registry datasource that owns *target* (an engine or a session factory), or ``None``."""
    if isinstance(target, AsyncEngine):
        return _BY_ENGINE.get(target)
    return _BY_SESSIONMAKER.get(target)


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class MeteredAsyncQueuePool(AsyncAdaptedQueuePool):
    """The queue pool of every registry engine that would get ``AsyncAdaptedQueuePool``: it times checkouts.

    An observer added with :meth:`add_acquire_observer` receives, for every checkout, the seconds it
    took to obtain its connection: the wait for an idle connection when the pool is busy, the connect
    when the pool grows or recycles one, and the pre-ping when it is on. A checkout that fails (a pool
    timeout) is reported too. The observers carry over to the pool ``engine.dispose()`` creates.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._acquire_observers: list[Callable[[float], None]] = []

    def add_acquire_observer(self, observer: Callable[[float], None]) -> None:
        """Call *observer* with the seconds each checkout took."""
        self._acquire_observers.append(observer)

    def connect(self) -> PoolProxiedConnection:
        observers = self._acquire_observers
        if not observers:
            return super().connect()
        started = time.perf_counter()
        try:
            return super().connect()
        finally:
            elapsed = time.perf_counter() - started
            for observer in observers:
                observer(elapsed)

    def recreate(self) -> MeteredAsyncQueuePool:
        pool = cast(MeteredAsyncQueuePool, super().recreate())
        pool._acquire_observers = self._acquire_observers  # shared: the observers follow the engine
        return pool


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataSourceCapabilities:
    """What a datasource's dialect and driver support, for the accelerators that are dialect-gated.

    ``fast_autocommit_reads`` is true only on PostgreSQL, where a single-statement read on an
    ``AUTOCOMMIT`` connection costs one round trip instead of three. ``isolation_levels`` are the
    transaction isolation levels the driver accepts (``AUTOCOMMIT`` excluded). ``max_in_params`` is the
    largest IN list a statement may bind. The ``*_returning`` flags come from the dialect and are final
    once the first connection has told SQLAlchemy the server version (MariaDB gained ``RETURNING`` in
    10.5); ``supports_returning`` means ``INSERT`` and ``UPDATE`` both support it.
    """

    dialect: str
    driver: str
    supports_savepoints: bool
    supports_returning: bool
    insert_returning: bool
    update_returning: bool
    delete_returning: bool
    fast_autocommit_reads: bool
    isolation_levels: frozenset[str]
    max_in_params: int

    def supports_isolation(self, level: str) -> bool:
        """Whether *level* (``"SERIALIZABLE"``, ``"read committed"``...) is available on this datasource."""
        return level.replace("_", " ").upper() in self.isolation_levels

    @classmethod
    def of(cls, dialect: Dialect) -> DataSourceCapabilities:
        """The capabilities of *dialect* as it stands (before or after its first connection)."""
        name = "mariadb" if getattr(dialect, "is_mariadb", False) else dialect.name
        try:
            # The level lists are static per driver; no live DBAPI connection is needed to read them.
            levels = frozenset(str(level) for level in dialect.get_isolation_level_values(cast(Any, None)))
        except Exception:  # noqa: BLE001 — a dialect that needs a live connection falls back to the table
            levels = _STATIC_ISOLATION_LEVELS.get(name, frozenset())
        insert_returning = bool(dialect.insert_returning)
        update_returning = bool(dialect.update_returning)
        if name == "sqlite":
            max_in = 32766 if sqlite3.sqlite_version_info >= (3, 32, 0) else 999
        else:
            max_in = _MAX_IN_PARAMS.get(name, 1000)
        return cls(
            dialect=name,
            driver=str(dialect.driver),
            supports_savepoints=name in _SAVEPOINT_DIALECTS,
            supports_returning=insert_returning and update_returning,
            insert_returning=insert_returning,
            update_returning=update_returning,
            delete_returning=bool(dialect.delete_returning),
            fast_autocommit_reads=name == "postgresql",
            isolation_levels=levels - {"AUTOCOMMIT"},
            max_in_params=max_in,
        )


# ---------------------------------------------------------------------------
# DataSource
# ---------------------------------------------------------------------------


class DataSource:
    """One database the application talks to: its engine, session factory and capabilities.

    ``name`` is the datasource's key (``"primary"``, a named datasource, or the name a module
    registered); a read replica carries its parent's name and ``is_replica``. ``url`` is the
    SQLAlchemy URL, whose ``repr``/``str`` mask the password (:attr:`masked_url` renders it masked).
    ``metadata`` is the slot for the framework tables that live on this datasource, and
    ``customizers`` are the after-begin customizers that apply to it.
    """

    def __init__(
        self,
        name: str,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        properties: DataSourceProperties,
        registry: DataSourceRegistry | None = None,
        url_key: str | None = None,
        replica: DataSource | None = None,
        is_replica: bool = False,
    ) -> None:
        self.name = name
        self.engine = engine
        self.sessionmaker = sessionmaker
        self.properties = properties
        self.url_key = url_key
        self.replica = replica
        self.is_replica = is_replica
        self.metadata = MetaData()
        self._registry_ref = weakref.ref(registry) if registry is not None else None
        self._customizers: list[AfterBeginCustomizer] = []
        self._capabilities: DataSourceCapabilities | None = None
        self._connected_with: tuple[str | None, str | None] = (self.url.username, self.url.password)
        # Bumped each time a credential rotation evicts the pool; a connection opened under an older
        # epoch is closed when it is returned.
        self._credentials_epoch = 0
        _BY_ENGINE[engine] = self
        _BY_SESSIONMAKER[sessionmaker] = self

    # -- identity -----------------------------------------------------------------------------------

    @property
    def url(self) -> URL:
        """The URL the engine was built with (its ``str`` masks the password)."""
        return self.engine.url

    @property
    def masked_url(self) -> str:
        """The URL with the password masked, for logs and diagnostics."""
        return self.url.render_as_string(hide_password=True)

    @property
    def qualified_name(self) -> str:
        """``name``, or ``name.replica`` for a read replica (health and metrics labels)."""
        return f"{self.name}.replica" if self.is_replica else self.name

    @property
    def registry(self) -> DataSourceRegistry | None:
        """The registry this datasource belongs to (``None`` once that registry is gone)."""
        return self._registry_ref() if self._registry_ref is not None else None

    @property
    def is_embedded(self) -> bool:
        """Whether the database runs in-process (SQLite)."""
        return self.url.get_backend_name() == "sqlite"

    @property
    def capabilities(self) -> DataSourceCapabilities:
        """The dialect's capabilities; recomputed until the first connection has initialized the dialect."""
        if self._capabilities is not None:
            return self._capabilities
        dialect = self.engine.dialect
        capabilities = DataSourceCapabilities.of(dialect)
        if dialect.server_version_info is not None:
            self._capabilities = capabilities
        return capabilities

    # -- units of work --------------------------------------------------------------------------------

    @property
    def customizers(self) -> tuple[AfterBeginCustomizer, ...]:
        """The after-begin customizers of this datasource, in order."""
        return tuple(self._customizers)

    def begin_options(self, *, read_only: bool) -> dict[str, Any]:
        """Execution options a unit applies before ``BEGIN`` (SQLite write units: ``BEGIN IMMEDIATE``)."""
        return begin_execution_options(self.url.get_backend_name(), read_only=read_only)

    async def after_begin(self, connection: AsyncSession | AsyncConnection) -> None:
        """Run the after-begin customizers on *connection*, which just began a transaction here."""
        await run_after_begin(self, connection)

    async def dispose(self) -> None:
        """Dispose this datasource's engine (and its replica's)."""
        await self.engine.dispose()
        if self.replica is not None:
            await self.replica.engine.dispose()

    def _add_customizer(self, customizer: AfterBeginCustomizer) -> None:
        if not any(existing is customizer for existing in self._customizers):
            self._customizers.append(customizer)
            self._customizers.sort(key=lambda item: get_order(type(item)))

    def __repr__(self) -> str:
        kind = ", replica" if self.is_replica else ""
        return f"DataSource(name={self.name!r}, url={self.masked_url!r}{kind})"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _url_identity(url: str | URL) -> tuple[Any, ...]:
    """What makes two URLs the same database: everything but the password (and SQLite paths absolute)."""
    parsed = make_url(url)
    database = parsed.database
    if (
        parsed.get_backend_name() == "sqlite"
        and database
        and is_file_database(parsed)
        and not database.startswith("file:")
    ):
        database = os.path.abspath(database)
    return (
        parsed.drivername,
        parsed.username,
        (parsed.host or "").lower(),
        parsed.port,
        database,
        tuple(sorted((key, str(value)) for key, value in parsed.query.items())),
    )


class DataSourceRegistry:
    """Every datasource of an application, built from configuration through one factory.

    ``config`` is read lazily: nothing is built until a datasource is first asked for. Pass
    ``properties`` to build from already-read settings instead of ``config``, and ``session_options``
    to change what every session factory is created with (the default is ``expire_on_commit=False``).
    """

    _instances: weakref.WeakKeyDictionary[Config, DataSourceRegistry] = weakref.WeakKeyDictionary()
    _instances_lock = threading.Lock()

    def __init__(
        self,
        config: Config | None = None,
        *,
        properties: RelationalProperties | None = None,
        session_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._config = config if config is not None else Config({})
        self._properties = properties
        self._session_options: dict[str, Any] = {"expire_on_commit": False, **(session_options or {})}
        self._lock = threading.RLock()
        self._loaded = False
        self._closed = False
        self._primary: DataSource | None = None
        self._primary_error: str | None = None
        self._named: dict[str, DataSource] = {}
        self._customizers: list[tuple[AfterBeginCustomizer, frozenset[str] | None]] = []
        self._credentials_providers: list[DataSourceCredentialsProvider] = []
        self._listeners: list[Callable[[DataSource], None]] = []

    # -- one registry per configuration ------------------------------------------------------------

    @classmethod
    def for_config(cls, config: Config) -> DataSourceRegistry:
        """The registry of *config*, created on first use; a closed one is replaced."""
        with cls._instances_lock:
            registry = cls._instances.get(config)
            if registry is None or registry.closed:
                registry = cls(config)
                cls._instances[config] = registry
            return registry

    # -- reading ------------------------------------------------------------------------------------

    @property
    def properties(self) -> RelationalProperties:
        """The relational settings every datasource is built from."""
        with self._lock:
            if self._properties is None:
                self._properties = RelationalProperties.from_config(self._config)
            return self._properties

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has disposed this registry."""
        return self._closed

    @property
    def primary(self) -> DataSource:
        """The primary datasource (``pyfly.data.relational.url``).

        Raises :class:`DataSourceConfigurationError` when no URL is configured, unless the ``dev``
        profile is active (then it is ``sqlite+aiosqlite:///./app.db``, with a warning).
        """
        self._load()
        if self._primary is None:
            raise DataSourceConfigurationError(self._primary_error or "No primary datasource is configured")
        return self._primary

    @property
    def has_primary(self) -> bool:
        """Whether a primary datasource is configured."""
        self._load()
        return self._primary is not None

    def get(self, name: str = PRIMARY) -> DataSource:
        """The datasource registered as *name* (``"primary"`` is the primary)."""
        if name == PRIMARY:
            return self.primary
        self._load()
        try:
            return self._named[name]
        except KeyError:
            raise NoSuchDataSourceError(f"No datasource named {name!r}; configured: {self.names()}") from None

    def names(self) -> list[str]:
        """The datasource names: ``primary`` first (when configured), then the others sorted."""
        self._load()
        names = sorted(self._named)
        return [PRIMARY, *names] if self._primary is not None else names

    def datasources(self) -> list[DataSource]:
        """Every datasource, in :meth:`names` order (replicas are reached through ``.replica``)."""
        return [self.get(name) for name in self.names()]

    def all_datasources(self) -> list[DataSource]:
        """Every datasource and every replica: what health checks, metrics and :meth:`close` cover."""
        out: list[DataSource] = []
        for datasource in self.datasources():
            out.append(datasource)
            if datasource.replica is not None:
                out.append(datasource.replica)
        return out

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.names()

    def __iter__(self) -> Iterator[DataSource]:
        return iter(self.datasources())

    def __len__(self) -> int:
        return len(self.names())

    def engine(self, name: str = PRIMARY) -> AsyncEngine:
        """The engine of datasource *name*."""
        return self.get(name).engine

    def session_factory(self, name: str = PRIMARY) -> async_sessionmaker[AsyncSession]:
        """The session factory of datasource *name*."""
        return self.get(name).sessionmaker

    def replica(self, name: str = PRIMARY) -> DataSource | None:
        """The read replica of datasource *name*, or ``None`` when it has none."""
        return self.get(name).replica

    def find_by_url(self, url: str | URL) -> DataSource | None:
        """The registered datasource (not a replica) whose URL is the same database as *url*."""
        identity = _url_identity(url)
        for datasource in self.datasources():
            if _url_identity(datasource.url) == identity:
                return datasource
        return None

    def find_by_engine(self, engine: AsyncEngine) -> DataSource | None:
        """The datasource (or replica) of this registry built on *engine*."""
        datasource = _BY_ENGINE.get(engine)
        return datasource if datasource is not None and datasource.registry is self else None

    # -- registering --------------------------------------------------------------------------------

    def register(
        self,
        name: str,
        url: str | URL,
        *,
        properties: DataSourceProperties | None = None,
        url_key: str | None = None,
    ) -> DataSource:
        """Build and register datasource *name* on *url* with the registry's treatment.

        *properties* default to the primary's settings for that URL (:meth:`RelationalProperties.derived`).
        *url_key* is the configuration key the URL came from; the credential hook re-reads it for every
        new connection. Registering an existing name with the same database returns that datasource; with
        another database it raises :class:`DataSourceConfigurationError`.
        """
        if name == PRIMARY:
            raise DataSourceConfigurationError(f"{PRIMARY!r} is reserved for the primary datasource")
        self._load()
        with self._lock:
            self._check_open()
            existing = self._named.get(name)
            if existing is not None:
                if _url_identity(existing.url) == _url_identity(url):
                    return existing
                raise DataSourceConfigurationError(
                    f"Datasource {name!r} is already registered on {existing.masked_url}; "
                    f"cannot register it again on {make_url(url).render_as_string(hide_password=True)}"
                )
            settings = properties or self.properties.derived(make_url(url).render_as_string(hide_password=False))
            datasource = self._build(name, url, settings, url_key=url_key)
            self._named[name] = datasource
        self._announce(datasource)
        return datasource

    def resolve(self, url: str | None, *, name: str, url_key: str | None = None) -> DataSource:
        """The datasource a module should use, given the URL it was configured with (if any).

        No URL: the primary. A URL of a registered datasource: that datasource (one engine per
        database). Another URL: a new datasource registered as *name*. *url_key* names the module's URL
        key, for error messages and for the credential hook.
        """
        if url is None or not str(url).strip():
            try:
                return self.primary
            except DataSourceConfigurationError as exc:
                hint = f"set {url_key} or {PREFIX}.url" if url_key else f"set {PREFIX}.url"
                raise DataSourceConfigurationError(f"{exc}. The {name!r} datasource has no URL: {hint}.") from None
        found = self.find_by_url(str(url))
        if found is not None:
            return found
        return self.register(name, str(url), url_key=url_key)

    def add_customizer(self, customizer: AfterBeginCustomizer, *, datasource: str | None = None) -> None:
        """Apply an after-begin customizer to *datasource* (and its replica), or to every datasource."""
        scope = None if datasource is None else frozenset({datasource})
        self.add_scoped_customizer(customizer, scope)

    def add_scoped_customizer(self, customizer: AfterBeginCustomizer, datasources: frozenset[str] | None) -> None:
        """Apply *customizer* to the datasources named in *datasources* (``None``: all, now and later)."""
        with self._lock:
            self._customizers.append((customizer, datasources))
            built = [ds for ds in (self._primary, *self._named.values()) if ds is not None]
        for datasource in built:
            for target in (datasource, datasource.replica):
                if target is not None and (datasources is None or target.name in datasources):
                    target._add_customizer(customizer)

    def add_credentials_provider(self, provider: DataSourceCredentialsProvider) -> None:
        """Consult *provider* for the credentials of every new connection (before the configuration).

        The provider is asked with the datasource's :attr:`~DataSource.qualified_name`: ``"primary"``,
        ``"reporting"``, and ``"primary.replica"`` for a read replica.
        """
        with self._lock:
            if not any(existing is provider for existing in self._credentials_providers):
                self._credentials_providers.append(provider)

    def on_register(self, listener: Callable[[DataSource], None]) -> None:
        """Call *listener* for every datasource and replica, those registered already and those to come."""
        with self._lock:
            self._listeners.append(listener)
        for datasource in self.all_datasources():
            listener(datasource)

    # -- credentials ------------------------------------------------------------------------------------

    async def refresh_credentials(self) -> list[str]:
        """Soft-evict the pools whose credentials changed since their connections were opened.

        Called on a configuration refresh. New connections already take the live credentials (the
        ``do_connect`` hook). Evicting a pool closes its idle connections at once and replaces the
        pool; a connection in use finishes its work and is closed when it is returned, instead of going
        back to a pool. Returns the evicted datasources (qualified names).
        """
        evicted: list[str] = []
        for datasource in self._built():
            live = self._live_credentials(datasource)
            if live is not None and live != datasource._connected_with:
                datasource._credentials_epoch += 1
                await datasource.engine.dispose()
                evicted.append(datasource.qualified_name)
                _logger.info("datasource_credentials_rotated", extra={"datasource": datasource.qualified_name})
        return evicted

    def _live_credentials(self, datasource: DataSource) -> tuple[str | None, str | None] | None:
        # Providers are asked by qualified name: a replica ("primary.replica") is not its primary.
        for provider in list(self._credentials_providers):
            credentials = provider.datasource_credentials(datasource.qualified_name)
            if credentials is not None:
                return credentials
        if datasource.url_key is None:
            return None
        raw = self._config.get(datasource.url_key)
        if raw is None or not str(raw).strip():
            return None
        live = make_url(str(raw).strip())
        return live.username, live.password

    # -- closing --------------------------------------------------------------------------------------

    async def close(self) -> None:
        """Dispose every engine (primary, replicas, named, module datasources) exactly once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            datasources = self._built()
        seen: set[int] = set()
        for datasource in datasources:
            if id(datasource.engine) in seen:
                continue
            seen.add(id(datasource.engine))
            try:
                await datasource.engine.dispose()
            except Exception:  # noqa: BLE001 — one failing pool must not keep the others open
                _logger.warning(
                    "datasource_dispose_failed", extra={"datasource": datasource.qualified_name}, exc_info=True
                )
        with DataSourceRegistry._instances_lock:
            for config, registry in list(DataSourceRegistry._instances.items()):
                if registry is self:
                    del DataSourceRegistry._instances[config]

    async def dispose_all(self) -> None:
        """Alias of :meth:`close`."""
        await self.close()

    # -- building -------------------------------------------------------------------------------------

    def _built(self) -> list[DataSource]:
        with self._lock:
            primaries = [ds for ds in (self._primary, *self._named.values()) if ds is not None]
        out: list[DataSource] = []
        for datasource in primaries:
            out.append(datasource)
            if datasource.replica is not None:
                out.append(datasource.replica)
        return out

    def _check_open(self) -> None:
        if self._closed:
            raise DataSourceConfigurationError("The datasource registry is closed (the application context stopped)")

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._check_open()
            props = self.properties
            primary_settings = props.primary()
            # The key the primary URL came from: the credential hook re-reads it for every connection.
            url_key: str | None = f"{PREFIX}.url"
            if self._config.get(f"{PREFIX}.url") is None and self._config.get("pyfly.data.url") is not None:
                url_key = "pyfly.data.url"
            if primary_settings.url is None and self._dev_profile():
                _logger.warning(
                    "No %s.url is configured; the dev profile falls back to %s (never used outside dev)",
                    PREFIX,
                    DEV_FALLBACK_URL,
                )
                primary_settings = props.derived(DEV_FALLBACK_URL)
                primary_settings.read_replica_url = props.read_replica.url
                url_key = None
            if primary_settings.url is not None:
                self._primary = self._build(
                    PRIMARY,
                    primary_settings.url,
                    primary_settings,
                    url_key=url_key,
                    replica_key=f"{PREFIX}.read-replica.url",
                )
            else:
                self._primary_error = (
                    f"No primary datasource: {PREFIX}.url is not configured "
                    f"(set it, or the PYFLY_DATA_RELATIONAL_URL environment variable)"
                )
            for name, settings in props.datasources.items():
                assert settings.url is not None  # from_config skips entries without a URL
                self._named[name] = self._build(
                    name,
                    settings.url,
                    settings,
                    url_key=f"{PREFIX}.datasources.{name}.url",
                    replica_key=f"{PREFIX}.datasources.{name}.read-replica.url",
                )
            self._loaded = True
            built = self._built()
        for datasource in built:
            self._announce_to_listeners(datasource)

    def _dev_profile(self) -> bool:
        from pyfly.context.environment import Environment

        try:
            return "dev" in Environment(self._config).active_profiles
        except Exception:  # noqa: BLE001 — an unreadable profile setting is not the dev profile
            return False

    def _build(
        self,
        name: str,
        url: str | URL,
        settings: DataSourceProperties,
        *,
        url_key: str | None,
        replica_key: str | None = None,
    ) -> DataSource:
        parsed = make_url(url)
        engine = self._create_engine(parsed, settings)
        datasource = DataSource(
            name,
            engine,
            async_sessionmaker(engine, **self._session_options),
            properties=settings,
            registry=self,
            url_key=url_key,
        )
        self._install_credentials(datasource, parsed, settings)
        if settings.read_replica_url:
            replica_settings = DataSourceProperties(
                url=settings.read_replica_url,
                echo=settings.echo,
                pool=settings.pool,
                connect_args=dict(settings.connect_args),
                sqlite=settings.sqlite,
            )
            replica_url = make_url(settings.read_replica_url)
            replica_engine = self._create_engine(replica_url, replica_settings)
            replica = DataSource(
                name,
                replica_engine,
                async_sessionmaker(replica_engine, **self._session_options),
                properties=replica_settings,
                registry=self,
                url_key=replica_key,
                is_replica=True,
            )
            self._install_credentials(replica, replica_url, replica_settings)
            datasource.replica = replica
        for customizer, scope in self._customizers:
            if scope is None or name in scope:
                datasource._add_customizer(customizer)
                if datasource.replica is not None:
                    datasource.replica._add_customizer(customizer)
        _logger.info(
            "datasource_registered",
            extra={"datasource": name, "url": datasource.masked_url, "dialect": engine.dialect.name},
        )
        return datasource

    def _create_engine(self, url: URL, settings: DataSourceProperties) -> AsyncEngine:
        backend = url.get_backend_name()
        pool = settings.pool
        connect_args = self._connect_args(url, settings)
        memory_sqlite = backend == "sqlite" and not is_file_database(url)
        kwargs: dict[str, Any] = {"echo": settings.echo, "pool_pre_ping": pool.pre_ping}
        if not memory_sqlite:
            # Recycling the single StaticPool connection of an in-memory database would drop the database.
            kwargs["pool_recycle"] = pool.recycle
        if connect_args:
            kwargs["connect_args"] = connect_args
        dialect_class: Any = url.get_dialect(_is_async=True)
        pool_class = cast(type, dialect_class.get_pool_class(url))
        if pool_class is AsyncAdaptedQueuePool:
            kwargs["poolclass"] = MeteredAsyncQueuePool
        sizing = {"pool_size": pool.size, "max_overflow": pool.max_overflow, "pool_timeout": pool.timeout}
        configured = {key: value for key, value in sizing.items() if value is not None}
        if configured:
            if issubclass(pool_class, QueuePool):
                kwargs.update(configured)
            else:
                _logger.warning(
                    "datasource_pool_sizing_ignored",
                    extra={"url": url.render_as_string(hide_password=True), "pool": pool_class.__name__},
                )
        engine = create_async_engine(url, **kwargs)
        if backend == "sqlite":
            explicit_timeout = "timeout" in connect_args or "timeout" in url.query
            install_sqlite_customizer(engine, settings.sqlite, explicit_timeout=explicit_timeout)
        return engine

    def _connect_args(self, url: URL, settings: DataSourceProperties) -> dict[str, Any]:
        args: dict[str, Any] = {
            key: (dict(value) if isinstance(value, Mapping) else value) for key, value in settings.connect_args.items()
        }
        if url.get_backend_name() == "postgresql":
            app_name = str(self._config.get("pyfly.app.name") or "pyfly")
            driver = url.get_driver_name()
            if driver == "asyncpg":
                server_settings = dict(args.get("server_settings") or {})
                server_settings.setdefault("application_name", app_name)
                args["server_settings"] = server_settings
            elif driver == "psycopg" and "application_name" not in url.query:
                args.setdefault("application_name", app_name)
        return args

    def _install_credentials(self, datasource: DataSource, url: URL, settings: DataSourceProperties) -> None:
        if url.get_backend_name() == "sqlite":
            return

        def _supplier() -> tuple[str | None, str | None] | None:
            return self._live_credentials(datasource)

        def _connected(credentials: tuple[str | None, str | None], record: ConnectionPoolEntry) -> None:
            datasource._connected_with = credentials
            record.info[_CREDENTIALS_EPOCH] = datasource._credentials_epoch

        def _returned(dbapi_connection: Any, record: ConnectionPoolEntry) -> None:
            # A connection opened before a rotation evicted its pool was in use then; it is closed now
            # rather than left in the replaced pool until the garbage collector finds it.
            epoch = datasource._credentials_epoch
            if dbapi_connection is not None and record.info.get(_CREDENTIALS_EPOCH, epoch) != epoch:
                record.invalidate()

        install_credentials_hook(
            datasource.engine, _supplier, explicit_keys=tuple(settings.connect_args), on_connect=_connected
        )
        event.listen(datasource.engine.sync_engine, "checkin", _returned)

    def _announce(self, datasource: DataSource) -> None:
        self._announce_to_listeners(datasource)
        if datasource.replica is not None:
            self._announce_to_listeners(datasource.replica)

    def _announce_to_listeners(self, datasource: DataSource) -> None:
        for listener in list(self._listeners):
            listener(datasource)

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("loaded" if self._loaded else "not loaded")
        return f"DataSourceRegistry({state}, datasources={list(self._named) if self._loaded else '?'})"
