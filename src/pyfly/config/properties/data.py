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
"""Relational data subsystem configuration properties (``pyfly.data.relational.*``).

:meth:`RelationalProperties.from_config` is how the framework reads these settings, and the
:class:`~pyfly.data.relational.datasource_registry.DataSourceRegistry` builds every engine from what it
returns. Each value is read through :meth:`Config.get` for its exact key, so a ``${...}`` placeholder
resolves and a ``PYFLY_*`` environment variable wins for every key, named datasources included. Each
value is then cast to its declared type with Config's truthy set: ``"false"`` is ``False``, and a value
that is neither a boolean nor a number where one is expected raises ``ValueError`` naming the key,
instead of being guessed (``bool("false")`` is ``True``, which turned SQL echo on).

Every datasource gets the same treatment: named datasources and the ones a module registers inherit
the top-level settings (echo, pool, SQLite setup, and the connect arguments when the driver is the
same) and override what they set themselves.

Deprecated aliases, honored with a warning when the ``relational`` key is absent:

==============================  =====================================
Legacy key                      Canonical key
==============================  =====================================
``pyfly.data.url``              ``pyfly.data.relational.url``
``pyfly.data.echo``             ``pyfly.data.relational.echo``
``pyfly.data.pool-size``        ``pyfly.data.relational.pool.size``
``pyfly.data.relational.pool-size``  ``pyfly.data.relational.pool.size``
==============================  =====================================
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, Literal

from pyfly.core.config import config_properties

if TYPE_CHECKING:
    from pyfly.core.config import Config

_logger = logging.getLogger(__name__)

PREFIX = "pyfly.data.relational"
"""The configuration prefix of the relational data subsystem."""

PRIMARY = "primary"
"""The name of the primary datasource. It is reserved: no named datasource may use it."""

EchoSetting = Literal["debug"] | bool
"""SQL echo: ``False``, ``True`` (statements) or ``"debug"`` (statements and result rows)."""

# Config's truthy set (core/config.py ``_coerce_like``), and its complement.
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off", ""})

# The PRAGMA values the SQLite customizer may interpolate; anything else is rejected at startup.
_JOURNAL_MODES = frozenset({"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"})
_SYNCHRONOUS_MODES = frozenset({"OFF", "NORMAL", "FULL", "EXTRA", "0", "1", "2", "3"})


# ---------------------------------------------------------------------------
# Casting
# ---------------------------------------------------------------------------


def parse_bool(value: Any, key: str) -> bool:
    """Cast *value* to ``bool`` with Config's truthy set; raise ``ValueError`` naming *key* otherwise."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    raise ValueError(f"{key} must be a boolean (true/false, yes/no, on/off, 1/0), got {value!r}")


def parse_int(value: Any, key: str) -> int:
    """Cast *value* to ``int``; booleans and non-integral values raise ``ValueError`` naming *key*."""
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{key} must be an integer, got {value!r}")


def parse_float(value: Any, key: str) -> float:
    """Cast *value* to ``float``; booleans and non-numeric values raise ``ValueError`` naming *key*."""
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{key} must be a number, got {value!r}")


def parse_echo(value: Any, key: str) -> EchoSetting:
    """Cast an ``echo`` setting: a boolean, or ``"debug"`` (SQLAlchemy also logs result rows then)."""
    if isinstance(value, str) and value.strip().lower() == "debug":
        return "debug"
    return parse_bool(value, key)


def _parse_choice(value: Any, key: str, allowed: frozenset[str]) -> str:
    text = str(value).strip().upper()
    if text not in allowed:
        raise ValueError(f"{key} must be one of {sorted(allowed)}, got {value!r}")
    return text


def _drivername(url: str | None) -> str | None:
    """The ``dialect+driver`` part of a URL (``postgresql+asyncpg``), or ``None``."""
    if not url or "://" not in url:
        return None
    return url.split("://", 1)[0].lower()


def _string_list(value: Any, key: str) -> list[str]:
    """A list of names: a YAML list, or one comma-separated string (an environment variable)."""
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(f"{key} must be a list of names, got {value!r}")
    return [str(item).strip() for item in items if str(item).strip()]


# ---------------------------------------------------------------------------
# Schema strategy (ddl-auto)
# ---------------------------------------------------------------------------

DDL_AUTO_STRATEGIES: tuple[str, ...] = ("none", "validate", "create", "create-drop")
"""The values ``pyfly.data.relational.ddl-auto`` accepts (see :func:`ddl_auto_strategy`)."""


def is_embedded_url(url: str | None) -> bool:
    """Whether *url* names a database that runs in the application's process (SQLite).

    No URL counts as one: the only primary a missing URL gets is the ``dev`` profile's SQLite file.
    """
    driver = _drivername(url)
    if driver is None:
        return not (url or "").strip()
    return driver.split("+", 1)[0] == "sqlite"


def ddl_auto_strategy(value: Any, *, url: str | None, migrations: bool = False) -> str:
    """The effective schema strategy of the primary datasource, from ``pyfly.data.relational.ddl-auto``.

    - Unset: ``none`` when startup migrations are enabled (they own the schema); otherwise ``create`` for an
      embedded database (SQLite, or no URL at all) and ``none`` for a database server, as Spring Boot does.
    - ``none``, ``validate``, ``create`` or ``create-drop``, in any case. ``false``, ``off`` and ``no`` (YAML
      turns the first two into a boolean) mean ``none``.
    - Anything else raises ``ValueError``. ``update`` does too: PyFly never alters an existing table at
      startup, so a missing column needs a migration.
    - ``create`` or ``create-drop`` beside ``pyfly.data.relational.migrations.enabled=true`` raises
      ``ValueError``: two schema managers would hide a missing migration until the deploy that adds it fails.
    """
    key = f"{PREFIX}.ddl-auto"
    if value is None or (isinstance(value, str) and not value.strip()):
        if migrations:
            return "none"
        return "create" if is_embedded_url(url) else "none"
    if value is False or (isinstance(value, str) and value.strip().lower() in _FALSE):
        return "none"
    if isinstance(value, bool):
        raise ValueError(f"{key} must be one of {', '.join(DDL_AUTO_STRATEGIES)}, got {value!r}")
    strategy = str(value).strip().lower()
    if strategy == "update":
        raise ValueError(
            f"{key}=update is not supported: PyFly never alters an existing table at startup. Generate a "
            "migration for the change (pyfly db migrate), or use create, which creates the missing tables only"
        )
    if strategy not in DDL_AUTO_STRATEGIES:
        raise ValueError(f"{key} must be one of {', '.join(DDL_AUTO_STRATEGIES)}, got {value!r}")
    if migrations and strategy in ("create", "create-drop"):
        raise ValueError(
            f"{key}={strategy} beside {PREFIX}.migrations.enabled=true: the tables create_all() adds hide a "
            "migration that is missing until the deploy that adds it fails. Let the migrations own the schema "
            f"(remove {key}: it is none then) or check it after them ({key}=validate)"
        )
    return strategy


# ---------------------------------------------------------------------------
# Property classes
# ---------------------------------------------------------------------------


@dataclass
class PoolProperties:
    """``pool.*`` — connection pool settings applied to every engine the registry builds.

    ``size``, ``max_overflow`` and ``timeout`` default to SQLAlchemy's own (5, 10, 30 s) and apply to
    queue pools only (the in-memory SQLite ``StaticPool`` has no size). ``recycle`` replaces a pooled
    connection after that many seconds (``-1`` never), which bounds how long a connection survives a
    rotated credential or a server-side idle timeout. ``pre_ping`` tests every checkout with a round trip;
    it stays off by default because it nearly doubles the cost of a short unit of work, and SQLAlchemy
    invalidates the pool on a disconnect anyway.
    """

    size: int | None = None
    max_overflow: int | None = None
    timeout: float | None = None
    recycle: int = 1800
    pre_ping: bool = False

    def __post_init__(self) -> None:
        self.size = None if self.size is None else parse_int(self.size, f"{PREFIX}.pool.size")
        self.max_overflow = (
            None if self.max_overflow is None else parse_int(self.max_overflow, f"{PREFIX}.pool.max-overflow")
        )
        self.timeout = None if self.timeout is None else parse_float(self.timeout, f"{PREFIX}.pool.timeout")
        self.recycle = parse_int(self.recycle, f"{PREFIX}.pool.recycle")
        self.pre_ping = parse_bool(self.pre_ping, f"{PREFIX}.pool.pre-ping")


@dataclass
class SqliteProperties:
    """``sqlite.*`` — connection setup for SQLite datasources.

    ``foreign_keys`` turns ``PRAGMA foreign_keys`` on for every connection (SQLite leaves it off, so
    constraints and ``ON DELETE CASCADE`` silently do nothing). ``journal_mode`` and ``synchronous`` apply
    to file databases only (WAL lets readers run beside a writer, and ``NORMAL`` is safe under WAL).
    ``busy_timeout`` (milliseconds) is how long a connection waits for a lock; it is left alone when the
    URL or the connect arguments set sqlite3's own ``timeout``.
    """

    foreign_keys: bool = True
    journal_mode: str = "WAL"
    synchronous: str = "NORMAL"
    busy_timeout: int = 5000

    def __post_init__(self) -> None:
        self.foreign_keys = parse_bool(self.foreign_keys, f"{PREFIX}.sqlite.foreign-keys")
        self.journal_mode = _parse_choice(self.journal_mode, f"{PREFIX}.sqlite.journal-mode", _JOURNAL_MODES)
        self.synchronous = _parse_choice(self.synchronous, f"{PREFIX}.sqlite.synchronous", _SYNCHRONOUS_MODES)
        self.busy_timeout = parse_int(self.busy_timeout, f"{PREFIX}.sqlite.busy-timeout")


@dataclass
class ReadReplicaProperties:
    """``read-replica.*`` — the read replica of a datasource (read-only units route to it)."""

    url: str | None = None


@dataclass
class HealthProperties:
    """``health.*`` — the ``db`` readiness check."""

    timeout: float = 2.0

    def __post_init__(self) -> None:
        self.timeout = parse_float(self.timeout, f"{PREFIX}.health.timeout")
        if self.timeout <= 0:
            raise ValueError(f"{PREFIX}.health.timeout must be positive, got {self.timeout!r}")


@dataclass
class MigrationsProperties:
    """``migrations.*`` — the Alembic migrations of the primary datasource.

    With ``enabled``, the context applies them when it starts (``alembic upgrade <revision>`` with the
    environment ``config`` names, ``alembic.ini`` by default), before the schema strategy runs. ``models``
    lists the modules that declare the application's entities: the ``env.py`` of ``pyfly db init`` imports
    them (a package with every module under it), so autogenerate compares the database with every model.
    """

    enabled: bool = False
    config: str = "alembic.ini"
    revision: str = "head"
    models: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.enabled = parse_bool(self.enabled, f"{PREFIX}.migrations.enabled")
        self.models = _string_list(self.models, f"{PREFIX}.migrations.models")


@dataclass
class SchemaProperties:
    """``schema.*`` — how the schema strategy and the migrations run.

    ``lock_timeout`` (seconds) bounds the wait for another instance that is changing the schema: instances
    that start together apply the migrations and ``ddl-auto`` one at a time. ``drop_timeout`` (seconds)
    bounds the ``create-drop`` teardown, including the wait for the database locks the drop needs.
    """

    lock_timeout: float = 300.0
    drop_timeout: float = 10.0

    def __post_init__(self) -> None:
        self.lock_timeout = parse_float(self.lock_timeout, f"{PREFIX}.schema.lock-timeout")
        self.drop_timeout = parse_float(self.drop_timeout, f"{PREFIX}.schema.drop-timeout")
        for name, value in (("lock-timeout", self.lock_timeout), ("drop-timeout", self.drop_timeout)):
            if value <= 0:
                raise ValueError(f"{PREFIX}.schema.{name} must be positive, got {value!r}")


@dataclass
class DataSourceProperties:
    """The effective settings of one datasource: its URL plus everything the engine is built with."""

    url: str | None = None
    # ``Literal`` first: ``Config.bind`` coerces the first member of a union, and "debug" must survive.
    echo: EchoSetting = False
    pool: PoolProperties = field(default_factory=PoolProperties)
    connect_args: dict[str, Any] = field(default_factory=dict)
    sqlite: SqliteProperties = field(default_factory=SqliteProperties)
    read_replica_url: str | None = None

    def __post_init__(self) -> None:
        self.echo = parse_echo(self.echo, f"{PREFIX}.echo")
        if isinstance(self.pool, Mapping):
            self.pool = PoolProperties(**_fields_of(PoolProperties, self.pool))
        if isinstance(self.sqlite, Mapping):
            self.sqlite = SqliteProperties(**_fields_of(SqliteProperties, self.sqlite))

    @property
    def drivername(self) -> str | None:
        """The URL's ``dialect+driver`` (``postgresql+asyncpg``), or ``None`` without a URL."""
        return _drivername(self.url)


@config_properties(prefix=PREFIX)
@dataclass
class RelationalProperties:
    """Configuration for the relational data subsystem (``pyfly.data.relational.*``).

    Build it with :meth:`from_config`; ``Config.bind`` (used by ``/actuator/configprops``) gives the same
    values for every ordinary spelling. :meth:`primary` and :meth:`derived` return the effective settings
    of the primary datasource and of an extra datasource a module registers.

    ``ddl_auto`` is the effective schema strategy (:func:`ddl_auto_strategy`): unset, it is ``create`` for an
    embedded database and ``none`` for a database server or when the migrations are enabled.
    """

    enabled: bool = False
    url: str | None = None
    echo: EchoSetting = False
    ddl_auto: str | None = None
    pool: PoolProperties = field(default_factory=PoolProperties)
    connect_args: dict[str, Any] = field(default_factory=dict)
    sqlite: SqliteProperties = field(default_factory=SqliteProperties)
    read_replica: ReadReplicaProperties = field(default_factory=ReadReplicaProperties)
    datasources: dict[str, DataSourceProperties] = field(default_factory=dict)
    health: HealthProperties = field(default_factory=HealthProperties)
    migrations: MigrationsProperties = field(default_factory=MigrationsProperties)
    schema: SchemaProperties = field(default_factory=SchemaProperties)
    #: Deprecated: ``pyfly.data.relational.pool-size`` was never read; it now aliases ``pool.size``.
    pool_size: int | None = None

    def __post_init__(self) -> None:
        # Config.bind hands raw strings and nested dicts to fields it cannot type; normalize them so the
        # bound object holds the same typed values as from_config().
        self.enabled = parse_bool(self.enabled, f"{PREFIX}.enabled")
        self.echo = parse_echo(self.echo, f"{PREFIX}.echo")
        if isinstance(self.migrations, Mapping):
            self.migrations = MigrationsProperties(**_fields_of(MigrationsProperties, self.migrations))
        if isinstance(self.schema, Mapping):
            self.schema = SchemaProperties(**_fields_of(SchemaProperties, self.schema))
        self.ddl_auto = ddl_auto_strategy(self.ddl_auto, url=self.url, migrations=self.migrations.enabled)
        self.datasources = {
            str(name): settings if isinstance(settings, DataSourceProperties) else _bound_datasource(settings)
            for name, settings in (self.datasources or {}).items()
            if isinstance(settings, (DataSourceProperties, Mapping))
        }
        if self.pool_size is not None:
            self.pool_size = parse_int(self.pool_size, f"{PREFIX}.pool-size")
            if self.pool.size is None:
                self.pool = replace(self.pool, size=self.pool_size)

    # -- effective datasource settings ---------------------------------------------------------------

    def primary(self) -> DataSourceProperties:
        """The effective settings of the primary datasource."""
        return DataSourceProperties(
            url=self.url,
            echo=self.echo,
            pool=self.pool,
            connect_args=dict(self.connect_args),
            sqlite=self.sqlite,
            read_replica_url=self.read_replica.url,
        )

    def derived(self, url: str) -> DataSourceProperties:
        """Settings for an extra datasource on *url*: the primary's, with its connect arguments only when
        *url* uses the same driver (asyncpg arguments would break asyncmy, and the other way round)."""
        same_driver = _drivername(url) == _drivername(self.url)
        return DataSourceProperties(
            url=url,
            echo=self.echo,
            pool=self.pool,
            connect_args=dict(self.connect_args) if same_driver else {},
            sqlite=self.sqlite,
        )

    # -- reading ------------------------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Config) -> RelationalProperties:
        """Read ``pyfly.data.relational.*`` from *config*, key by key, with typed casting."""
        reader = _Reader(config)

        url = reader.string(f"{PREFIX}.url")
        if url is None:
            url = reader.legacy_string("pyfly.data.url", f"{PREFIX}.url")
        raw_echo = reader.raw(f"{PREFIX}.echo")
        if raw_echo is None:
            raw_echo = reader.legacy_raw("pyfly.data.echo", f"{PREFIX}.echo")
        echo = parse_echo(raw_echo, f"{PREFIX}.echo") if raw_echo is not None else False

        pool = reader.pool(f"{PREFIX}.pool", PoolProperties())
        if pool.size is None:
            for legacy_key in (f"{PREFIX}.pool-size", "pyfly.data.pool-size"):
                legacy_size = reader.legacy_raw(legacy_key, f"{PREFIX}.pool.size")
                if legacy_size is not None:
                    pool = replace(pool, size=parse_int(legacy_size, legacy_key))
                    break

        connect_args = reader.tree(f"{PREFIX}.connect-args")
        sqlite = reader.sqlite(f"{PREFIX}.sqlite", SqliteProperties())

        migrations = MigrationsProperties(
            enabled=parse_bool(reader.raw(f"{PREFIX}.migrations.enabled", False), f"{PREFIX}.migrations.enabled"),
            config=reader.string(f"{PREFIX}.migrations.config") or "alembic.ini",
            revision=reader.string(f"{PREFIX}.migrations.revision") or "head",
            models=_string_list(reader.raw(f"{PREFIX}.migrations.models"), f"{PREFIX}.migrations.models"),
        )
        schema = SchemaProperties(
            lock_timeout=reader.raw(f"{PREFIX}.schema.lock-timeout", 300.0),
            drop_timeout=reader.raw(f"{PREFIX}.schema.drop-timeout", 10.0),
        )

        props = cls(
            enabled=parse_bool(reader.raw(f"{PREFIX}.enabled", False), f"{PREFIX}.enabled"),
            url=url,
            echo=echo,
            ddl_auto=ddl_auto_strategy(reader.raw(f"{PREFIX}.ddl-auto"), url=url, migrations=migrations.enabled),
            pool=pool,
            connect_args=connect_args,
            sqlite=sqlite,
            read_replica=ReadReplicaProperties(url=reader.string(f"{PREFIX}.read-replica.url")),
            health=HealthProperties(
                timeout=parse_float(reader.raw(f"{PREFIX}.health.timeout", 2.0), f"{PREFIX}.health.timeout")
            ),
            migrations=migrations,
            schema=schema,
        )
        props.datasources = reader.datasources(props)
        return props


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def _fields_of(cls: type, mapping: Mapping[str, Any]) -> dict[str, Any]:
    """The entries of a bound mapping that are fields of dataclass *cls*, keys relaxed (``max-overflow``)."""
    names = {f.name for f in fields(cls)}
    relaxed = {str(key).replace("-", "_").lower(): value for key, value in mapping.items()}
    return {key: value for key, value in relaxed.items() if key in names}


def _bound_datasource(mapping: Mapping[str, Any]) -> DataSourceProperties:
    """A named datasource as ``Config.bind`` hands it over (a raw mapping), typed."""
    values = _fields_of(DataSourceProperties, mapping)
    replica = {str(key).replace("-", "_").lower(): value for key, value in mapping.items()}.get("read_replica")
    if isinstance(replica, Mapping):
        values["read_replica_url"] = replica.get("url")
    return DataSourceProperties(**values)


class _Reader:
    """Reads configuration values one exact key at a time, so every key gets placeholders and env."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def raw(self, key: str, default: Any = None) -> Any:
        value = self._config.get(key)
        return default if value is None else value

    def string(self, key: str) -> str | None:
        value = self._config.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def legacy_raw(self, legacy: str, canonical: str) -> Any:
        value = self._config.get(legacy)
        if value is not None:
            _logger.warning("%s is deprecated and will be removed; use %s", legacy, canonical)
        return value

    def legacy_string(self, legacy: str, canonical: str) -> str | None:
        value = self.legacy_raw(legacy, canonical)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def pool(self, base: str, inherited: PoolProperties) -> PoolProperties:
        values: dict[str, Any] = {}
        for attr, key in (
            ("size", "size"),
            ("max_overflow", "max-overflow"),
            ("timeout", "timeout"),
            ("recycle", "recycle"),
            ("pre_ping", "pre-ping"),
        ):
            raw = self._config.get(f"{base}.{key}")
            if raw is None:
                continue
            full = f"{base}.{key}"
            if attr == "timeout":
                values[attr] = parse_float(raw, full)
            elif attr == "pre_ping":
                values[attr] = parse_bool(raw, full)
            else:
                values[attr] = parse_int(raw, full)
        return replace(inherited, **values)

    def sqlite(self, base: str, inherited: SqliteProperties) -> SqliteProperties:
        values: dict[str, Any] = {}
        for attr, key in (
            ("foreign_keys", "foreign-keys"),
            ("journal_mode", "journal-mode"),
            ("synchronous", "synchronous"),
            ("busy_timeout", "busy-timeout"),
        ):
            raw = self._config.get(f"{base}.{key}")
            if raw is None:
                continue
            full = f"{base}.{key}"
            if attr == "foreign_keys":
                values[attr] = parse_bool(raw, full)
            elif attr == "journal_mode":
                values[attr] = _parse_choice(raw, full, _JOURNAL_MODES)
            elif attr == "synchronous":
                values[attr] = _parse_choice(raw, full, _SYNCHRONOUS_MODES)
            else:
                values[attr] = parse_int(raw, full)
        return replace(inherited, **values)

    def tree(self, base: str) -> dict[str, Any]:
        """A free-form subtree (driver connect arguments): keys verbatim, every leaf read by exact key."""
        raw = self._config.get(base)
        if not isinstance(raw, Mapping):
            return {}
        return self._resolve(base, raw)

    def _resolve(self, base: str, node: Mapping[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, value in node.items():
            path = f"{base}.{key}"
            if isinstance(value, Mapping):
                resolved[str(key)] = self._resolve(path, value)
            else:
                leaf = self._config.get(path)
                resolved[str(key)] = value if leaf is None else leaf
        return resolved

    def datasources(self, parent: RelationalProperties) -> dict[str, DataSourceProperties]:
        """``datasources.<name>.*``: each named datasource inherits the top-level settings.

        The names come from the YAML subtree and from ``PYFLY_DATA_RELATIONAL_DATASOURCES_<NAME>_*``
        environment variables (a datasource may be declared in the environment alone); every value is
        then read by its exact key.
        """
        raw = self._config.get(f"{PREFIX}.datasources")
        declared = [str(name) for name in raw] if isinstance(raw, Mapping) else []
        # Env-only names are split on every underscore, so a variable that overrides "event-store"
        # also yields a stray "event" entry; an env-only name counts only when it carries a URL.
        env_only = [
            str(name) for name in self._config.effective_section(f"{PREFIX}.datasources") if str(name) not in declared
        ]
        parent_driver = _drivername(parent.url)
        result: dict[str, DataSourceProperties] = {}
        for name in [*declared, *env_only]:
            if name == PRIMARY:
                raise ValueError(
                    f"{PREFIX}.datasources.{PRIMARY} is reserved for the primary datasource; "
                    f"configure it with {PREFIX}.url, or give the named datasource another name"
                )
            base = f"{PREFIX}.datasources.{name}"
            url = self.string(f"{base}.url")
            if url is None:
                if name in declared:
                    _logger.warning("Named datasource %r has no %s.url; it is skipped", name, base)
                continue
            raw_echo = self._config.get(f"{base}.echo")
            own_args = self.tree(f"{base}.connect-args")
            inherited_args = dict(parent.connect_args) if _drivername(url) == parent_driver else {}
            result[name] = DataSourceProperties(
                url=url,
                echo=parent.echo if raw_echo is None else parse_echo(raw_echo, f"{base}.echo"),
                pool=self.pool(f"{base}.pool", parent.pool),
                connect_args={**inherited_args, **own_args},
                sqlite=self.sqlite(f"{base}.sqlite", parent.sqlite),
                read_replica_url=self.string(f"{base}.read-replica.url"),
            )
        return result
