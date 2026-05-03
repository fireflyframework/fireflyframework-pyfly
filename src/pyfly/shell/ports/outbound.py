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
"""Outbound port: shell runner interface."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pyfly.shell.result import ShellParam


@runtime_checkable
class ShellRunnerPort(Protocol):
    """Abstract shell runner interface.

    Any shell adapter (Click, Typer, etc.) must implement this protocol.
    """

    def register_command(
        self,
        key: str,
        handler: Callable[..., Any],
        *,
        help_text: str = "",
        group: str = "",
        params: list[ShellParam] | None = None,
    ) -> None: ...

    async def run(self, args: list[str] | None = None) -> int: ...

    async def run_interactive(self) -> None: ...
