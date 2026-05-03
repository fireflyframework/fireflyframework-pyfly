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
"""Tests for ShellRunnerPort protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyfly.shell.ports.outbound import ShellRunnerPort
from pyfly.shell.result import ShellParam


class TestShellRunnerPortProtocol:
    def test_conforming_class_is_instance(self) -> None:
        class FakeRunner:
            def register_command(
                self,
                key: str,
                handler: Callable[..., Any],
                *,
                help_text: str = "",
                group: str = "",
                params: list[ShellParam] | None = None,
            ) -> None:
                pass

            async def run(self, args: list[str] | None = None) -> int:
                return 0

            async def run_interactive(self) -> None:
                pass

        assert isinstance(FakeRunner(), ShellRunnerPort)

    def test_non_conforming_class_is_not_instance(self) -> None:
        class Incomplete:
            def register_command(
                self,
                key: str,
                handler: Callable[..., Any],
            ) -> None:
                pass

        assert not isinstance(Incomplete(), ShellRunnerPort)
