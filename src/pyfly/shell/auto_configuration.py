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
"""Shell subsystem auto-configuration."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.shell.ports.outbound import ShellRunnerPort


@auto_configuration
@conditional_on_property("pyfly.shell.enabled", having_value="true")
@conditional_on_missing_bean(ShellRunnerPort)
class ShellAutoConfiguration:
    """Auto-configures the Click shell adapter when shell is enabled."""

    @bean
    def shell_runner(self) -> ShellRunnerPort:
        from pyfly.shell.adapters.click_adapter import ClickShellAdapter

        return ClickShellAdapter()
