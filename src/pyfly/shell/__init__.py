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
"""PyFly Shell — CLI application framework with Spring Shell-style DI integration.

Build CLI applications using @shell_component classes and @shell_method commands
with full dependency injection, interactive REPL, and CommandLineRunner support.
"""

from pyfly.container.stereotypes import shell_component
from pyfly.shell.decorators import shell_argument, shell_method, shell_method_availability, shell_option
from pyfly.shell.ports.outbound import ShellRunnerPort
from pyfly.shell.result import CommandResult, ShellParam
from pyfly.shell.runner import ApplicationArguments, ApplicationRunner, CommandLineRunner

__all__ = [
    "ApplicationArguments",
    "ApplicationRunner",
    "CommandLineRunner",
    "CommandResult",
    "ShellParam",
    "ShellRunnerPort",
    "shell_argument",
    "shell_component",
    "shell_method",
    "shell_method_availability",
    "shell_option",
]
