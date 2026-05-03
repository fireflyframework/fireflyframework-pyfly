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
"""Tests for ShellAutoConfiguration."""

from __future__ import annotations

from pyfly.shell.auto_configuration import ShellAutoConfiguration
from pyfly.shell.ports.outbound import ShellRunnerPort


class TestShellAutoConfiguration:
    def test_has_auto_configuration_marker(self) -> None:
        assert getattr(ShellAutoConfiguration, "__pyfly_auto_configuration__", False) is True

    def test_has_configuration_stereotype(self) -> None:
        assert getattr(ShellAutoConfiguration, "__pyfly_stereotype__", None) == "configuration"

    def test_has_on_property_condition(self) -> None:
        conditions = getattr(ShellAutoConfiguration, "__pyfly_conditions__", [])
        types = [c["type"] for c in conditions]
        assert "on_property" in types

    def test_has_on_missing_bean_condition(self) -> None:
        conditions = getattr(ShellAutoConfiguration, "__pyfly_conditions__", [])
        types = [c["type"] for c in conditions]
        assert "on_missing_bean" in types

    def test_shell_runner_produces_shell_runner_port(self) -> None:
        config = ShellAutoConfiguration()
        runner = config.shell_runner()
        assert isinstance(runner, ShellRunnerPort)
