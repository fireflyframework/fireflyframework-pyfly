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
"""Tests for @config_properties dataclass binding per subsystem."""

from pyfly.config.properties import (
    CacheProperties,
    ClientProperties,
    LoggingProperties,
    MessagingProperties,
    RelationalProperties,
    WebProperties,
)
from pyfly.core.config import Config


class TestWebProperties:
    def test_bind_defaults(self):
        config = Config({"pyfly": {"web": {}}})
        props = config.bind(WebProperties)
        assert props.port == 8000
        assert props.host == "0.0.0.0"
        assert props.debug is False

    def test_bind_custom_values(self):
        config = Config({"pyfly": {"web": {"port": 9090, "host": "127.0.0.1", "debug": True}}})
        props = config.bind(WebProperties)
        assert props.port == 9090
        assert props.host == "127.0.0.1"
        assert props.debug is True


class TestRelationalProperties:
    def test_bind_defaults(self):
        config = Config({"pyfly": {"data": {"relational": {}}}})
        props = config.bind(RelationalProperties)
        assert props.enabled is False
        assert props.url == "sqlite+aiosqlite:///pyfly.db"
        assert props.echo is False
        assert props.pool_size == 5

    def test_bind_enabled(self):
        config = Config({"pyfly": {"data": {"relational": {"enabled": True, "url": "postgresql://localhost/db"}}}})
        props = config.bind(RelationalProperties)
        assert props.enabled is True
        assert props.url == "postgresql://localhost/db"


class TestCacheProperties:
    def test_bind_defaults(self):
        config = Config({"pyfly": {"cache": {}}})
        props = config.bind(CacheProperties)
        assert props.enabled is False
        assert props.provider == "auto"
        assert props.ttl == 300

    def test_bind_enabled_redis(self):
        config = Config({"pyfly": {"cache": {"enabled": True, "provider": "redis", "ttl": 600}}})
        props = config.bind(CacheProperties)
        assert props.enabled is True
        assert props.provider == "redis"
        assert props.ttl == 600


class TestMessagingProperties:
    def test_bind_defaults(self):
        config = Config({"pyfly": {"messaging": {}}})
        props = config.bind(MessagingProperties)
        assert props.provider == "auto"

    def test_bind_kafka(self):
        config = Config({"pyfly": {"messaging": {"provider": "kafka"}}})
        props = config.bind(MessagingProperties)
        assert props.provider == "kafka"


class TestClientProperties:
    def test_bind_defaults(self):
        config = Config({"pyfly": {"client": {}}})
        props = config.bind(ClientProperties)
        assert props.timeout == 30

    def test_bind_custom_timeout(self):
        config = Config({"pyfly": {"client": {"timeout": 60}}})
        props = config.bind(ClientProperties)
        assert props.timeout == 60


class TestLoggingProperties:
    def test_bind_defaults(self):
        config = Config({"pyfly": {"logging": {}}})
        props = config.bind(LoggingProperties)
        assert props.format == "console"

    def test_bind_json_format(self):
        config = Config({"pyfly": {"logging": {"format": "json"}}})
        props = config.bind(LoggingProperties)
        assert props.format == "json"
