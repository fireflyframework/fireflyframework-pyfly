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
"""Tests for property placeholder resolution in Config values."""

import pytest

from pyfly.core.config import Config


class TestPlaceholderResolution:
    """Config.get() should resolve ${...} placeholders in string values."""

    def test_resolve_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "s3cret")
        config = Config({"db": {"password": "${MY_SECRET}"}})
        assert config.get("db.password") == "s3cret"

    def test_resolve_config_reference(self):
        config = Config(
            {
                "app": {"name": "MyApp"},
                "greeting": "Hello from ${app.name}",
            }
        )
        assert config.get("greeting") == "Hello from MyApp"

    def test_resolve_with_default(self):
        config = Config({"key": "${MISSING_VAR:fallback_value}"})
        assert config.get("key") == "fallback_value"

    def test_resolve_nested(self):
        """Recursive resolution: placeholder value itself contains a placeholder."""
        config = Config(
            {
                "base": "localhost",
                "host": "${base}",
                "url": "http://${host}:8080",
            }
        )
        assert config.get("url") == "http://localhost:8080"

    def test_no_placeholder_passthrough(self):
        config = Config({"key": "plain-value"})
        assert config.get("key") == "plain-value"

    def test_non_string_passthrough(self):
        config = Config({"port": 8080})
        assert config.get("port") == 8080

    def test_env_var_in_placeholder(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "prod-db.example.com")
        config = Config({"database": {"url": "postgresql://${DB_HOST}:5432/mydb"}})
        assert config.get("database.url") == "postgresql://prod-db.example.com:5432/mydb"

    def test_multiple_placeholders_in_one_value(self, monkeypatch):
        monkeypatch.setenv("DB_USER", "admin")
        monkeypatch.setenv("DB_PASS", "secret")
        config = Config({"dsn": "${DB_USER}:${DB_PASS}@localhost"})
        assert config.get("dsn") == "admin:secret@localhost"

    def test_max_recursion_guard(self):
        """Circular references should not infinite-loop."""
        config = Config({"a": "${b}", "b": "${a}"})
        with pytest.raises(ValueError, match="[Mm]ax.*recursion|[Cc]ircular"):
            config.get("a")
