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
"""App port/host resolution — pyfly.server.port/.host (Spring server.* parity).

``pyfly.web.port`` / ``pyfly.web.host`` were removed (breaking); the app port is
now driven solely by the Spring-parity ``pyfly.server.*`` keys.
"""

from __future__ import annotations

import pytest

from pyfly.config.properties.server import ServerProperties, resolve_app_host, resolve_app_port
from pyfly.core.config import Config


def test_default_app_port_is_8080() -> None:
    assert resolve_app_port(Config({})) == 8080


def test_server_port_from_config() -> None:
    assert resolve_app_port(Config({"pyfly": {"server": {"port": 9000}}})) == 9000


def test_server_port_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYFLY_SERVER_PORT", "9001")
    assert resolve_app_port(Config({"pyfly": {"server": {"port": 8080}}})) == 9001


def test_default_app_host_is_wildcard() -> None:
    assert resolve_app_host(Config({})) == "0.0.0.0"


def test_server_host_from_config() -> None:
    assert resolve_app_host(Config({"pyfly": {"server": {"host": "127.0.0.1"}}})) == "127.0.0.1"


def test_server_properties_bind_port_host() -> None:
    props = ServerProperties()
    assert props.port == 8080
    assert props.host == "0.0.0.0"
    bound = Config({"pyfly": {"server": {"port": 9000, "host": "127.0.0.1"}}}).bind(ServerProperties)
    assert bound.port == 9000
    assert bound.host == "127.0.0.1"
