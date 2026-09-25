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
"""Typed ``pyfly.data.relational.*`` properties: casting, placeholders, env overrides and legacy keys.

Every datasource the registry builds reads its settings through :meth:`RelationalProperties.from_config`,
so these tests pin what an operator's configuration actually turns into:

- ``echo`` from an env var or a ``${...}`` placeholder is a string, and ``bool("false")`` is ``True``;
  the typed reader uses Config's truthy set and rejects anything else (C114).
- Named datasources are read key by key through ``Config.get``, so ``${...}`` placeholders resolve and
  ``PYFLY_*`` overrides win, exactly as they do for the primary (C046).
- The documented ``pyfly.data.url`` / ``pyfly.data.echo`` / ``pyfly.data.pool-size`` keys, which nothing
  read, are honored as deprecated aliases instead of being silently ignored (C043).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from pyfly.config.properties.data import (
    DataSourceProperties,
    PoolProperties,
    RelationalProperties,
    SqliteProperties,
)
from pyfly.core.config import Config


def _config(relational: dict[str, Any] | None = None, **data: Any) -> Config:
    tree: dict[str, Any] = dict(data)
    if relational is not None:
        tree["relational"] = relational
    return Config({"pyfly": {"data": tree}})


class TestDefaults:
    def test_nothing_configured(self) -> None:
        props = RelationalProperties.from_config(Config({}))
        assert props.enabled is False
        assert props.url is None  # no invented URL: the registry decides what a missing URL means
        assert props.echo is False
        assert props.ddl_auto == "create"
        assert props.pool == PoolProperties(size=None, max_overflow=None, timeout=None, recycle=1800, pre_ping=False)
        assert props.sqlite == SqliteProperties(
            foreign_keys=True, journal_mode="WAL", synchronous="NORMAL", busy_timeout=5000
        )
        assert props.connect_args == {}
        assert props.datasources == {}
        assert props.read_replica.url is None
        assert props.health.timeout == 2.0

    def test_pool_pre_ping_is_off_and_recycle_is_bounded_by_default(self) -> None:
        # Spec decision: pre-ping costs a round trip per checkout; recycle bounds a connection's age instead.
        pool = RelationalProperties.from_config(_config({"url": "sqlite+aiosqlite://"})).pool
        assert pool.pre_ping is False
        assert pool.recycle == 1800


class TestEchoCasting:
    """C114: the string ``"false"`` must never turn SQL echo on."""

    @pytest.mark.parametrize("raw", ["false", "False", "0", "no", "off", ""])
    def test_falsy_strings_from_yaml(self, raw: str) -> None:
        assert RelationalProperties.from_config(_config({"echo": raw})).echo is False

    @pytest.mark.parametrize("raw", ["true", "1", "yes", "on", True])
    def test_truthy_values(self, raw: Any) -> None:
        assert RelationalProperties.from_config(_config({"echo": raw})).echo is True

    def test_env_override_false_without_a_yaml_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_ECHO", "false")
        assert RelationalProperties.from_config(Config({})).echo is False

    def test_placeholder_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DB_ECHO", raising=False)
        assert RelationalProperties.from_config(_config({"echo": "${DB_ECHO:false}"})).echo is False
        monkeypatch.setenv("DB_ECHO", "true")
        assert RelationalProperties.from_config(_config({"echo": "${DB_ECHO:false}"})).echo is True

    def test_debug_is_kept(self) -> None:
        # SQLAlchemy's echo="debug" also logs result rows; it must not collapse to True.
        assert RelationalProperties.from_config(_config({"echo": "debug"})).echo == "debug"

    def test_garbage_is_rejected_with_the_key(self) -> None:
        with pytest.raises(ValueError, match=r"pyfly\.data\.relational\.echo"):
            RelationalProperties.from_config(_config({"echo": "sometimes"}))


class TestPoolCasting:
    def test_env_strings_are_cast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_POOL_SIZE", "12")
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_POOL_MAX_OVERFLOW", "3")
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_POOL_TIMEOUT", "2.5")
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_POOL_RECYCLE", "600")
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_POOL_PRE_PING", "on")
        pool = RelationalProperties.from_config(Config({})).pool
        assert pool == PoolProperties(size=12, max_overflow=3, timeout=2.5, recycle=600, pre_ping=True)

    def test_yaml_values_are_cast(self) -> None:
        pool = RelationalProperties.from_config(
            _config({"pool": {"size": "7", "max-overflow": 0, "timeout": "1", "pre-ping": "yes"}})
        ).pool
        assert (pool.size, pool.max_overflow, pool.timeout, pool.pre_ping) == (7, 0, 1.0, True)

    def test_non_numeric_size_is_rejected_with_the_key(self) -> None:
        with pytest.raises(ValueError, match=r"pyfly\.data\.relational\.pool\.size"):
            RelationalProperties.from_config(_config({"pool": {"size": "ten"}}))

    def test_boolean_is_not_a_number(self) -> None:
        with pytest.raises(ValueError, match=r"pool\.size"):
            RelationalProperties.from_config(_config({"pool": {"size": True}}))


class TestConnectArgs:
    def test_passed_through_with_types_and_nesting(self) -> None:
        props = RelationalProperties.from_config(
            _config(
                {
                    "url": "postgresql+asyncpg://app@db/orders",
                    "connect-args": {"statement_cache_size": 0, "server_settings": {"search_path": "app"}},
                }
            )
        )
        assert props.connect_args == {"statement_cache_size": 0, "server_settings": {"search_path": "app"}}

    def test_keys_keep_their_spelling(self) -> None:
        # Driver keyword arguments are not configuration keys: no kebab/snake or case rewriting.
        props = RelationalProperties.from_config(_config({"connect-args": {"TrustServerCertificate": "yes"}}))
        assert props.connect_args == {"TrustServerCertificate": "yes"}

    def test_placeholders_and_env_overrides_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PG_APP", "billing")
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_CONNECT_ARGS_STATEMENT_CACHE_SIZE", "0")
        props = RelationalProperties.from_config(
            _config(
                {
                    "connect-args": {
                        "statement_cache_size": 100,
                        "server_settings": {"application_name": "${PG_APP}"},
                    }
                }
            )
        )
        assert props.connect_args == {"statement_cache_size": 0, "server_settings": {"application_name": "billing"}}


class TestSqlite:
    def test_overrides(self) -> None:
        props = RelationalProperties.from_config(
            _config(
                {
                    "sqlite": {
                        "foreign-keys": "false",
                        "journal-mode": "delete",
                        "synchronous": "full",
                        "busy-timeout": "250",
                    }
                }
            )
        )
        assert props.sqlite == SqliteProperties(
            foreign_keys=False, journal_mode="DELETE", synchronous="FULL", busy_timeout=250
        )

    @pytest.mark.parametrize(("key", "value"), [("journal-mode", "wal; DROP TABLE x"), ("synchronous", "sometimes")])
    def test_pragma_values_are_allowlisted(self, key: str, value: str) -> None:
        with pytest.raises(ValueError, match=key.replace("-", r"[-_]")):
            RelationalProperties.from_config(_config({"sqlite": {key: value}}))


class TestNamedDatasources:
    """C046: named datasource URLs must resolve placeholders and honor PYFLY_* overrides."""

    def test_placeholder_in_named_url_resolves(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("REPORTING_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PASSWORD", "s3cret")
        props = RelationalProperties.from_config(
            _config(
                {
                    "datasources": {
                        "reporting": {"url": "sqlite+aiosqlite:///${REPORTING_DIR}/reporting.db"},
                        "pg": {"url": "postgresql+asyncpg://app:${DB_PASSWORD}@db/orders"},
                    }
                }
            )
        )
        assert props.datasources["reporting"].url == f"sqlite+aiosqlite:///{tmp_path}/reporting.db"
        assert props.datasources["pg"].url == "postgresql+asyncpg://app:s3cret@db/orders"

    def test_env_override_of_named_url_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_DATASOURCES_ANALYTICS_URL", f"sqlite+aiosqlite:///{tmp_path}/a.db")
        props = RelationalProperties.from_config(
            _config({"datasources": {"analytics": {"url": "sqlite+aiosqlite:///./overridden.db"}}})
        )
        assert props.datasources["analytics"].url == f"sqlite+aiosqlite:///{tmp_path}/a.db"

    def test_named_datasources_inherit_the_primary_settings(self) -> None:
        props = RelationalProperties.from_config(
            _config(
                {
                    "url": "postgresql+asyncpg://app@db/orders",
                    "echo": "true",
                    "pool": {"size": 8, "recycle": 900},
                    "connect-args": {"statement_cache_size": 0},
                    "datasources": {
                        "reporting": {"url": "postgresql+asyncpg://app@db/reporting", "pool": {"size": 2}},
                        "legacy": {"url": "mysql+asyncmy://app@db/legacy", "echo": False},
                    },
                }
            )
        )
        reporting = props.datasources["reporting"]
        assert reporting.echo is True
        assert reporting.pool.size == 2 and reporting.pool.recycle == 900
        assert reporting.connect_args == {"statement_cache_size": 0}  # same driver: inherited
        legacy = props.datasources["legacy"]
        assert legacy.echo is False
        assert legacy.connect_args == {}  # another driver: asyncpg arguments would break asyncmy

    def test_named_echo_false_string_is_false(self) -> None:
        props = RelationalProperties.from_config(
            _config({"datasources": {"reporting": {"url": "sqlite+aiosqlite://", "echo": "false"}}})
        )
        assert props.datasources["reporting"].echo is False

    def test_entry_without_url_is_skipped_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="pyfly.config.properties.data"):
            props = RelationalProperties.from_config(_config({"datasources": {"broken": {"echo": True}}}))
        assert props.datasources == {}
        assert "broken" in caplog.text

    def test_primary_is_a_reserved_name(self) -> None:
        with pytest.raises(ValueError, match="primary"):
            RelationalProperties.from_config(_config({"datasources": {"primary": {"url": "sqlite+aiosqlite://"}}}))

    def test_named_read_replica(self) -> None:
        props = RelationalProperties.from_config(
            _config(
                {
                    "datasources": {
                        "reporting": {
                            "url": "sqlite+aiosqlite:///r.db",
                            "read-replica": {"url": "sqlite+aiosqlite:///r2.db"},
                        }
                    }
                }
            )
        )
        assert props.datasources["reporting"].read_replica_url == "sqlite+aiosqlite:///r2.db"


class TestLegacyKeys:
    """C043: the keys the docs taught (``pyfly.data.url`` & co.) were read by nothing."""

    def test_pyfly_data_url_is_an_alias(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="pyfly.config.properties.data"):
            props = RelationalProperties.from_config(_config(url="postgresql+asyncpg://app@db/orders"))
        assert props.url == "postgresql+asyncpg://app@db/orders"
        assert "pyfly.data.url" in caplog.text and "deprecated" in caplog.text

    def test_env_pyfly_data_url_is_an_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PYFLY_DATA_URL", "postgresql+asyncpg://app@db/orders")
        assert RelationalProperties.from_config(Config({})).url == "postgresql+asyncpg://app@db/orders"

    def test_relational_url_wins_over_the_alias(self) -> None:
        props = RelationalProperties.from_config(
            _config({"url": "sqlite+aiosqlite:///primary.db"}, url="postgresql+asyncpg://app@db/orders")
        )
        assert props.url == "sqlite+aiosqlite:///primary.db"

    def test_pool_size_and_echo_aliases(self) -> None:
        props = RelationalProperties.from_config(_config(echo="true", **{"pool-size": "20"}))
        assert props.pool.size == 20
        assert props.echo is True

    def test_relational_pool_size_key_is_an_alias_of_pool_size(self) -> None:
        props = RelationalProperties.from_config(_config({"pool-size": 9}))
        assert props.pool.size == 9
        assert RelationalProperties.from_config(_config({"pool-size": 9, "pool": {"size": 4}})).pool.size == 4

    def test_framework_defaults_carry_no_database_url(self) -> None:
        # The defaults used to ship pyfly.data.url=sqlite:///pyfly.db; honoring the alias would then
        # point every application at a file in its working directory.
        defaults = Config(Config._load_framework_defaults())
        assert RelationalProperties.from_config(defaults).url is None


class TestPrimaryDatasource:
    def test_primary_carries_the_top_level_settings(self) -> None:
        props = RelationalProperties.from_config(
            _config(
                {
                    "url": "sqlite+aiosqlite:///p.db",
                    "echo": "debug",
                    "pool": {"size": 3},
                    "read-replica": {"url": "sqlite+aiosqlite:///r.db"},
                }
            )
        )
        primary = props.primary()
        assert isinstance(primary, DataSourceProperties)
        assert primary.url == "sqlite+aiosqlite:///p.db"
        assert primary.echo == "debug"
        assert primary.pool.size == 3
        assert primary.read_replica_url == "sqlite+aiosqlite:///r.db"

    def test_derived_keeps_settings_and_drops_foreign_connect_args(self) -> None:
        props = RelationalProperties.from_config(
            _config({"url": "postgresql+asyncpg://app@db/orders", "connect-args": {"statement_cache_size": 0}})
        )
        same_driver = props.derived("postgresql+asyncpg://app@db/events")
        other_driver = props.derived("sqlite+aiosqlite:///events.db")
        assert same_driver.connect_args == {"statement_cache_size": 0}
        assert other_driver.connect_args == {}
        assert same_driver.pool == props.pool


class TestConfigBind:
    """``Config.bind`` (``/actuator/configprops``) reports the same typed values the engines use."""

    def test_bind_reports_typed_named_datasources(self) -> None:
        config = _config(
            {
                "url": "sqlite+aiosqlite:///p.db",
                "echo": "false",
                "datasources": {"reporting": {"url": "sqlite+aiosqlite:///r.db", "pool": {"size": "2"}}},
            }
        )
        bound = config.bind(RelationalProperties)
        assert bound.echo is False
        assert isinstance(bound.datasources["reporting"], DataSourceProperties)
        assert bound.datasources["reporting"].pool.size == 2
