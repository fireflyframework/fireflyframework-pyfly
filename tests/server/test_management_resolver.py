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
"""Tests for the management-server mode resolver."""

from __future__ import annotations

from pyfly.core.config import Config
from pyfly.server.management_server import resolve_management_mode


def _cfg(port: object) -> Config:
    if port is None:
        return Config({})
    return Config({"pyfly": {"management": {"server": {"port": port}}}})


def test_unset_is_shared() -> None:
    mode, _ = resolve_management_mode(_cfg(None), main_port=8080)
    assert mode == "shared"


def test_equal_to_main_is_shared() -> None:
    mode, _ = resolve_management_mode(_cfg(8080), main_port=8080)
    assert mode == "shared"


def test_minus_one_is_disabled() -> None:
    mode, _ = resolve_management_mode(_cfg(-1), main_port=8080)
    assert mode == "disabled"


def test_different_positive_is_separate() -> None:
    mode, props = resolve_management_mode(_cfg(9090), main_port=8080)
    assert mode == "separate"
    assert props.port == 9090
