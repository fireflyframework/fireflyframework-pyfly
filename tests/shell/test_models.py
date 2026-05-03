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
"""Tests for ShellParam and CommandResult data models."""

import dataclasses

import pytest

from pyfly.shell.result import MISSING, CommandResult, ShellParam

# ---------------------------------------------------------------------------
# ShellParam
# ---------------------------------------------------------------------------


class TestShellParam:
    def test_positional_param(self):
        p = ShellParam(name="name", param_type=str, is_option=False)
        assert p.name == "name"
        assert p.param_type is str
        assert p.is_option is False
        assert p.default is MISSING

    def test_flag_param(self):
        p = ShellParam(name="verbose", param_type=bool, is_option=True, is_flag=True)
        assert p.is_flag is True
        assert p.is_option is True

    def test_option_with_default(self):
        p = ShellParam(name="count", param_type=int, is_option=True, default=10)
        assert p.default == 10

    def test_param_with_choices(self):
        p = ShellParam(
            name="color",
            param_type=str,
            is_option=True,
            choices=["red", "green", "blue"],
        )
        assert p.choices == ["red", "green", "blue"]

    def test_is_frozen_dataclass(self):
        p = ShellParam(name="x", param_type=str, is_option=False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_success_by_default(self):
        r = CommandResult()
        assert r.exit_code == 0
        assert r.is_success is True

    def test_failure(self):
        r = CommandResult(output="boom", exit_code=1)
        assert r.is_success is False
        assert r.output == "boom"

    def test_default_exit_code_is_zero(self):
        r = CommandResult(output="ok")
        assert r.exit_code == 0
