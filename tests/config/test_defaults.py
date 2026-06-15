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
"""Tests for framework defaults loading and TOML config support."""

from pathlib import Path

from pyfly.core.config import Config


class TestFrameworkDefaults:
    def test_load_defaults_provides_pyfly_namespace(self):
        defaults = Config._load_framework_defaults()
        assert "pyfly" in defaults
        assert "app" in defaults["pyfly"]

    def test_defaults_have_app_name(self):
        defaults = Config._load_framework_defaults()
        assert defaults["pyfly"]["app"]["name"] == "pyfly-app"

    def test_defaults_have_logging_level(self):
        defaults = Config._load_framework_defaults()
        assert defaults["pyfly"]["logging"]["level"]["root"] == "INFO"

    def test_defaults_have_server_port(self):
        defaults = Config._load_framework_defaults()
        assert defaults["pyfly"]["server"]["port"] == 8080

    def test_defaults_have_management_server_port(self):
        defaults = Config._load_framework_defaults()
        assert defaults["pyfly"]["management"]["server"]["port"] == 9090

    def test_defaults_data_disabled(self):
        defaults = Config._load_framework_defaults()
        assert defaults["pyfly"]["data"]["enabled"] is False

    def test_defaults_cache_disabled(self):
        defaults = Config._load_framework_defaults()
        assert defaults["pyfly"]["cache"]["enabled"] is False

    def test_from_file_loads_defaults_automatically(self, tmp_path: Path):
        config_file = tmp_path / "pyfly.yaml"
        config_file.write_text("myapp:\n  custom: true\n")
        config = Config.from_file(config_file)
        # Framework defaults should be present
        assert config.get("pyfly.app.name") == "pyfly-app"
        # User config should also be present
        assert config.get("myapp.custom") is True

    def test_user_config_overrides_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyfly.yaml"
        config_file.write_text("pyfly:\n  app:\n    name: my-custom-app\n")
        config = Config.from_file(config_file)
        assert config.get("pyfly.app.name") == "my-custom-app"

    def test_from_file_without_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyfly.yaml"
        config_file.write_text("app:\n  name: test\n")
        config = Config.from_file(config_file, load_defaults=False)
        # Framework defaults should NOT be present
        assert config.get("pyfly.app.name") is None
        # User config should still work
        assert config.get("app.name") == "test"

    def test_missing_file_returns_defaults_only(self, tmp_path: Path):
        config = Config.from_file(tmp_path / "nonexistent.yaml")
        assert config.get("pyfly.app.name") == "pyfly-app"


class TestTomlConfig:
    def test_load_toml_file(self, tmp_path: Path):
        config_file = tmp_path / "pyfly.toml"
        config_file.write_text('[pyfly.app]\nname = "toml-app"\nversion = "2.0.0"\n')
        config = Config.from_file(config_file, load_defaults=False)
        assert config.get("pyfly.app.name") == "toml-app"
        assert config.get("pyfly.app.version") == "2.0.0"

    def test_toml_with_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyfly.toml"
        config_file.write_text('[pyfly.app]\nname = "toml-app"\n')
        config = Config.from_file(config_file)
        # Overridden by TOML
        assert config.get("pyfly.app.name") == "toml-app"
        # From defaults
        assert config.get("pyfly.server.port") == 8080

    def test_toml_profile_overlay(self, tmp_path: Path):
        base = tmp_path / "pyfly.toml"
        base.write_text('[pyfly.app]\nname = "base-app"\n')
        profile = tmp_path / "pyfly-dev.toml"
        profile.write_text('[pyfly.app]\nname = "dev-app"\ndebug = true\n')
        config = Config.from_file(base, active_profiles=["dev"], load_defaults=False)
        assert config.get("pyfly.app.name") == "dev-app"
        assert config.get("pyfly.app.debug") is True

    def test_toml_nested_tables(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[server]\nport = 9090\nhost = "0.0.0.0"\n\n[server.pool]\nsize = 20\n')
        config = Config.from_file(config_file, load_defaults=False)
        assert config.get("server.port") == 9090
        assert config.get("server.pool.size") == 20


class TestConfigEnvVarNormalization:
    def test_pyfly_prefix_stripped_for_env_var(self, monkeypatch):
        monkeypatch.setenv("PYFLY_APP_NAME", "env-app")
        config = Config({})
        # pyfly.app.name should resolve to PYFLY_APP_NAME (not PYFLY_PYFLY_APP_NAME)
        assert config.get("pyfly.app.name") == "env-app"

    def test_non_pyfly_prefix_still_works(self, monkeypatch):
        monkeypatch.setenv("PYFLY_APP_NAME", "env-app")
        config = Config({})
        assert config.get("app.name") == "env-app"

    def test_hyphenated_keys_resolve_env_vars(self, monkeypatch):
        monkeypatch.setenv("PYFLY_DATA_POOL_SIZE", "20")
        config = Config({})
        assert config.get("pyfly.data.pool-size") == "20"


class TestDefaultsNoInfrastructure:
    """Framework defaults must not depend on external infrastructure."""

    def test_messaging_defaults_to_memory(self):
        config = Config(Config._load_framework_defaults())
        assert config.get("pyfly.messaging.provider") == "memory"

    def test_cache_defaults_to_memory(self):
        config = Config(Config._load_framework_defaults())
        assert config.get("pyfly.cache.provider") == "memory"


class TestMultiSourceConfig:
    """Config should load and merge from multiple sources."""

    def test_loads_from_config_dir_and_root(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: from-config-dir\n")
        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    version: '2.0'\n")

        config = Config.from_sources(tmp_path)
        assert config.get("pyfly.app.name") == "from-config-dir"
        assert config.get("pyfly.app.version") == "2.0"

    def test_root_overrides_config_dir(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: config-dir\n  server:\n    port: 8080\n")
        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: root-override\n")

        config = Config.from_sources(tmp_path)
        assert config.get("pyfly.app.name") == "root-override"  # root wins
        assert config.get("pyfly.server.port") == 8080  # from config dir

    def test_profile_overlays_from_both_locations(self, tmp_path: Path):
        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: base\n  server:\n    port: 8080\n")
        (tmp_path / "pyfly-dev.yaml").write_text("pyfly:\n  server:\n    port: 9090\n")

        config = Config.from_sources(tmp_path, active_profiles=["dev"])
        assert config.get("pyfly.app.name") == "base"
        assert config.get("pyfly.server.port") == 9090

    def test_tracks_loaded_sources(self, tmp_path: Path):
        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: tracked\n")

        config = Config.from_sources(tmp_path)
        assert len(config.loaded_sources) >= 2  # defaults + pyfly.yaml
        assert any("pyfly-defaults" in s for s in config.loaded_sources)
        assert any("pyfly.yaml" in s for s in config.loaded_sources)

    def test_toml_and_yaml_merged(self, tmp_path: Path):
        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: from-yaml\n")
        (tmp_path / "pyfly.toml").write_text("[pyfly.server]\nport = 9999\n")

        config = Config.from_sources(tmp_path)
        assert config.get("pyfly.app.name") == "from-yaml"
        assert config.get("pyfly.server.port") == 9999

    def test_config_subdir_profile_overlay(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "pyfly.yaml").write_text("pyfly:\n  app:\n    name: config-dir\n")
        (config_dir / "pyfly-prod.yaml").write_text("pyfly:\n  server:\n    port: 443\n")
        (tmp_path / "pyfly.yaml").write_text("pyfly:\n  server:\n    port: 8080\n")

        config = Config.from_sources(tmp_path, active_profiles=["prod"])
        assert config.get("pyfly.app.name") == "config-dir"
        assert config.get("pyfly.server.port") == 443

    def test_empty_dir_returns_defaults(self, tmp_path: Path):
        config = Config.from_sources(tmp_path)
        # Should have loaded only defaults
        assert config.get("pyfly.app.name") == "pyfly-app"  # from defaults
        assert len(config.loaded_sources) == 1
