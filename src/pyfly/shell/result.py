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
"""Shell parameter and command result data models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class _MissingSentinel:
    """Sentinel indicating no default value was provided."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _MissingSentinel()


@dataclass(frozen=True)
class ShellParam:
    """Describes a single parameter for a shell command."""

    name: str
    param_type: type
    is_option: bool
    default: Any = MISSING
    help_text: str = ""
    choices: list[str] | None = None
    is_flag: bool = False


@dataclass
class CommandResult:
    """The result of executing a shell command."""

    output: str = ""
    exit_code: int = 0

    @property
    def is_success(self) -> bool:
        """Return ``True`` when the command exited cleanly."""
        return self.exit_code == 0
