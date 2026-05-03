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
"""Web subsystem configuration properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.web")
@dataclass
class WebProperties:
    """Configuration for the web subsystem (pyfly.web.*)."""

    adapter: str = "auto"
    port: int = 8000
    host: str = "0.0.0.0"
    debug: bool = False
    docs: dict[str, Any] = field(default_factory=lambda: {"enabled": True})
    actuator: dict[str, Any] = field(default_factory=lambda: {"enabled": False})
