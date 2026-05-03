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
"""Admin dashboard configuration properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.admin")
@dataclass
class AdminProperties:
    """Configuration for the admin dashboard (pyfly.admin.*)."""

    enabled: bool = True
    path: str = "/admin"
    title: str = "PyFly Admin"
    theme: str = "auto"
    require_auth: bool = False
    allowed_roles: list[str] = field(default_factory=lambda: ["ADMIN"])
    refresh_interval: int = 5000


@config_properties(prefix="pyfly.admin.server")
@dataclass
class AdminServerProperties:
    """Configuration for admin server mode (pyfly.admin.server.*)."""

    enabled: bool = False
    poll_interval: int = 10000
    connect_timeout: int = 2000
    read_timeout: int = 5000
    instances: list[dict[str, Any]] = field(default_factory=list)


@config_properties(prefix="pyfly.admin.client")
@dataclass
class AdminClientProperties:
    """Configuration for admin client registration (pyfly.admin.client.*)."""

    url: str = ""
    auto_register: bool = False
