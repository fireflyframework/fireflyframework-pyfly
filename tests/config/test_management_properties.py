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
"""Tests for ManagementServerProperties binding (pyfly.management.server.*)."""

from __future__ import annotations

import pytest

from pyfly.config.properties import ManagementServerProperties
from pyfly.core.config import Config


def test_defaults() -> None:
    props = ManagementServerProperties()
    assert props.port is None
    assert props.address is None
    assert props.base_path == ""


def test_binds_from_config() -> None:
    cfg = Config({"pyfly": {"management": {"server": {"port": 9090, "address": "127.0.0.1", "base-path": "/mgmt"}}}})
    props = cfg.bind(ManagementServerProperties)
    assert props.port == 9090
    assert props.address == "127.0.0.1"
    assert props.base_path == "/mgmt"


def test_binds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYFLY_MANAGEMENT_SERVER_PORT", "9091")
    props = Config({}).bind(ManagementServerProperties)
    assert props.port == 9091
