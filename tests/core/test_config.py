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
"""Tests for configuration system."""

import os
from dataclasses import dataclass
from pathlib import Path

from pyfly.core.config import Config, config_properties


class TestConfig:
    def test_load_from_dict(self):
        config = Config({"app": {"name": "test-service", "port": 8080}})
        assert config.get("app.name") == "test-service"
        assert config.get("app.port") == 8080

    def test_get_with_default(self):
        config = Config({})
        assert config.get("missing.key", "default") == "default"

    def test_get_nested_value(self):
        config = Config({"database": {"pool": {"size": 10}}})
        assert config.get("database.pool.size") == 10

    def test_load_from_yaml_file(self, tmp_path: Path):
        yaml_content = "app:\n  name: my-service\n  port: 9090\n"
        config_file = tmp_path / "pyfly.yaml"
        config_file.write_text(yaml_content)
        config = Config.from_file(config_file)
        assert config.get("app.name") == "my-service"

    def test_env_var_override(self):
        os.environ["PYFLY_APP_NAME"] = "env-service"
        try:
            config = Config({"app": {"name": "file-service"}})
            assert config.get("app.name") == "env-service"
        finally:
            del os.environ["PYFLY_APP_NAME"]


class TestConfigProperties:
    def test_bind_to_dataclass(self):
        @config_properties(prefix="database")
        @dataclass
        class DatabaseConfig:
            url: str = "sqlite:///test.db"
            pool_size: int = 5

        config = Config({"database": {"url": "postgresql://localhost/mydb", "pool_size": 20}})
        db_config = config.bind(DatabaseConfig)
        assert db_config.url == "postgresql://localhost/mydb"
        assert db_config.pool_size == 20

    def test_bind_uses_defaults(self):
        @config_properties(prefix="database")
        @dataclass
        class DatabaseConfig:
            url: str = "sqlite:///default.db"
            pool_size: int = 5

        config = Config({})
        db_config = config.bind(DatabaseConfig)
        assert db_config.url == "sqlite:///default.db"
        assert db_config.pool_size == 5


class TestProfileConfigMerging:
    def test_merge_profile_config(self, tmp_path):
        base = tmp_path / "pyfly.yaml"
        base.write_text("server:\n  port: 8080\n  host: localhost\n")

        profile = tmp_path / "pyfly-dev.yaml"
        profile.write_text("server:\n  port: 9090\n  debug: true\n")

        config = Config.from_file(base, active_profiles=["dev"])
        assert config.get("server.port") == 9090
        assert config.get("server.host") == "localhost"
        assert config.get("server.debug") is True

    def test_merge_multiple_profiles(self, tmp_path):
        base = tmp_path / "pyfly.yaml"
        base.write_text("app:\n  name: test\n")

        dev = tmp_path / "pyfly-dev.yaml"
        dev.write_text("app:\n  debug: true\n")

        local = tmp_path / "pyfly-local.yaml"
        local.write_text("app:\n  port: 3000\n")

        config = Config.from_file(base, active_profiles=["dev", "local"])
        assert config.get("app.name") == "test"
        assert config.get("app.debug") is True
        assert config.get("app.port") == 3000

    def test_later_profile_wins(self, tmp_path):
        base = tmp_path / "pyfly.yaml"
        base.write_text("db:\n  url: base\n")

        dev = tmp_path / "pyfly-dev.yaml"
        dev.write_text("db:\n  url: dev-url\n")

        local = tmp_path / "pyfly-local.yaml"
        local.write_text("db:\n  url: local-url\n")

        config = Config.from_file(base, active_profiles=["dev", "local"])
        assert config.get("db.url") == "local-url"

    def test_missing_profile_file_is_skipped(self, tmp_path):
        base = tmp_path / "pyfly.yaml"
        base.write_text("app:\n  name: test\n")

        config = Config.from_file(base, active_profiles=["nonexistent"])
        assert config.get("app.name") == "test"

    def test_no_profiles_loads_base_only(self, tmp_path):
        base = tmp_path / "pyfly.yaml"
        base.write_text("app:\n  name: base\n")

        config = Config.from_file(base, active_profiles=[])
        assert config.get("app.name") == "base"

    def test_env_vars_still_win(self, tmp_path, monkeypatch):
        base = tmp_path / "pyfly.yaml"
        base.write_text("app:\n  name: base\n")

        profile = tmp_path / "pyfly-dev.yaml"
        profile.write_text("app:\n  name: dev\n")

        monkeypatch.setenv("PYFLY_APP_NAME", "env-wins")
        config = Config.from_file(base, active_profiles=["dev"])
        assert config.get("app.name") == "env-wins"


class TestRelaxedBinding:
    """Spring Boot relaxed binding: kebab-case YAML keys bind to snake_case fields."""

    def test_kebab_yaml_binds_to_snake_field(self):
        @config_properties(prefix="pyfly.server")
        @dataclass
        class Srv:
            graceful_timeout: int = 30
            event_loop: str = "auto"

        config = Config({"pyfly": {"server": {"graceful-timeout": 99, "event-loop": "uvloop"}}})
        srv = config.bind(Srv)
        assert srv.graceful_timeout == 99
        assert srv.event_loop == "uvloop"

    def test_kebab_binds_into_nested_dataclass(self):
        @dataclass
        class Inner:
            runtime_threads: int = 1

        @config_properties(prefix="pyfly.server")
        @dataclass
        class Outer:
            granian: Inner = None  # type: ignore[assignment]

        config = Config({"pyfly": {"server": {"granian": {"runtime-threads": 8}}}})
        outer = config.bind(Outer)
        assert outer.granian.runtime_threads == 8


class TestEnvBindingAndCoercion:
    """Env-var overrides must be visible to bind() and type-coerced (Spring relaxed binding)."""

    def test_env_override_visible_to_bind_and_coerced(self, monkeypatch):
        @config_properties(prefix="database")
        @dataclass
        class DB:
            pool_size: int = 5

        monkeypatch.setenv("PYFLY_DATABASE_POOL_SIZE", "20")
        config = Config({"database": {"pool_size": 5}})
        db = config.bind(DB)
        assert db.pool_size == 20

    def test_get_coerces_env_to_int_like_existing(self, monkeypatch):
        monkeypatch.setenv("PYFLY_WEB_PORT", "9000")
        config = Config({"web": {"port": 8080}})
        assert config.get("web.port") == 9000

    def test_get_coerces_env_to_bool_like_existing(self, monkeypatch):
        monkeypatch.setenv("PYFLY_WEB_DEBUG", "true")
        config = Config({"web": {"debug": False}})
        assert config.get("web.debug") is True

    def test_get_env_without_existing_value_returns_string(self, monkeypatch):
        monkeypatch.setenv("PYFLY_APP_NAME", "env-service")
        config = Config({})
        assert config.get("app.name") == "env-service"


class TestEffectiveAndSources:
    """Effective (resolved) view + ordered property sources, like /actuator/env."""

    def test_effective_dict_applies_env_override(self, monkeypatch):
        monkeypatch.setenv("PYFLY_APP_NAME", "envname")
        config = Config({"app": {"name": "filename"}})
        eff = config.effective_dict()
        assert eff["app"]["name"] == "envname"

    def test_effective_dict_resolves_placeholders(self):
        config = Config({"app": {"name": "svc", "label": "${app.name}-prod"}})
        eff = config.effective_dict()
        assert eff["app"]["label"] == "svc-prod"

    def test_property_sources_ordered_env_first(self, monkeypatch):
        monkeypatch.setenv("PYFLY_APP_NAME", "envname")
        config = Config({"app": {"name": "filename"}})
        sources = config.property_sources()
        names = [s["name"] for s in sources]
        assert any("systemEnvironment" in n for n in names)
        # systemEnvironment must outrank application config (appear first).
        env_idx = next(i for i, n in enumerate(names) if "systemEnvironment" in n)
        cfg_idx = next(i for i, n in enumerate(names) if "systemEnvironment" not in n)
        assert env_idx < cfg_idx

    def test_property_sources_attribute_value_and_origin(self):
        config = Config({"app": {"name": "filename"}})
        sources = config.property_sources()
        flat = {k: v for s in sources for k, v in s["properties"].items()}
        assert "app.name" in flat
        assert flat["app.name"]["value"] == "filename"


class TestSecretMasking:
    def test_masks_sensitive_keys(self):
        config = Config({})
        assert config.mask_value("pyfly.security.jwt.secret", "abc") == "******"
        assert config.mask_value("db.password", "hunter2") == "******"
        assert config.mask_value("api.token", "xyz") == "******"

    def test_does_not_mask_normal_keys(self):
        config = Config({})
        assert config.mask_value("pyfly.web.port", 8080) == 8080
        assert config.mask_value("app.name", "svc") == "svc"

    def test_redacts_password_in_uri_value(self):
        config = Config({})
        masked = config.mask_value("pyfly.data.url", "postgresql://user:hunter2@localhost/db")
        assert masked == "postgresql://user:******@localhost/db"
        # No userinfo password -> unchanged.
        assert config.mask_value("pyfly.data.url", "sqlite:///pyfly.db") == "sqlite:///pyfly.db"
