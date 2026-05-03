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
"""Runner protocols and ApplicationArguments for CLI startup hooks.

Mirrors Spring Boot's ``CommandLineRunner``, ``ApplicationRunner``, and
``ApplicationArguments`` so that beans can participate in post-startup
logic with either raw or parsed command-line arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ApplicationArguments:
    """Parsed representation of command-line arguments.

    Separates raw CLI tokens into *option* arguments (those starting with
    ``--``) and *non-option* arguments (everything else).
    """

    source_args: list[str] = field(default_factory=list)
    option_args: list[str] = field(default_factory=list)
    non_option_args: list[str] = field(default_factory=list)

    @classmethod
    def from_args(cls, args: list[str]) -> ApplicationArguments:
        """Parse raw CLI args into option (``--key=value``, ``--flag``) and non-option groups."""
        options = [a for a in args if a.startswith("--")]
        non_options = [a for a in args if not a.startswith("--")]
        return cls(source_args=list(args), option_args=options, non_option_args=non_options)

    def contains_option(self, name: str) -> bool:
        """Check if ``--name`` or ``--name=value`` is present."""
        prefix = f"--{name}"
        return any(opt == prefix or opt.startswith(f"{prefix}=") for opt in self.option_args)

    def get_option_values(self, name: str) -> list[str]:
        """Get all values for ``--name=value`` options."""
        prefix = f"--{name}="
        return [opt[len(prefix) :] for opt in self.option_args if opt.startswith(prefix)]


@runtime_checkable
class CommandLineRunner(Protocol):
    """Receives raw CLI args after the application context has started."""

    async def run(self, args: list[str]) -> None: ...


@runtime_checkable
class ApplicationRunner(Protocol):
    """Receives parsed :class:`ApplicationArguments` after the application context has started."""

    async def run(self, args: ApplicationArguments) -> None: ...
