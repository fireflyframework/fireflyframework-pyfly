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
"""Remote (--url) introspection via a stubbed actuator client."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pyfly.cli import introspect_cmds


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    class _Stub:
        def __init__(self, url: str, **k) -> None:
            calls["url"] = url

        def get(self, endpoint: str):
            calls["endpoint"] = endpoint
            return {"ok": True, "endpoint": endpoint}

    monkeypatch.setattr(introspect_cmds, "ActuatorClient", _Stub)
    return calls


def test_routes_remote(stub_client) -> None:
    result = CliRunner().invoke(introspect_cmds.routes_cmd, ["--url", "http://h:8080", "--json"])
    assert result.exit_code == 0, result.output
    assert stub_client["endpoint"] == "mappings"


def test_health_remote(stub_client) -> None:
    result = CliRunner().invoke(introspect_cmds.health_cmd, ["--url", "http://h:8080", "--json"])
    assert result.exit_code == 0, result.output
    assert stub_client["endpoint"] == "health"


def test_actuator_passthrough_remote(stub_client) -> None:
    result = CliRunner().invoke(introspect_cmds.actuator_cmd, ["info", "--url", "http://h:8080", "--json"])
    assert result.exit_code == 0, result.output
    assert stub_client["endpoint"] == "info"


def test_actuator_requires_url() -> None:
    result = CliRunner().invoke(introspect_cmds.actuator_cmd, ["info"])
    assert result.exit_code != 0
