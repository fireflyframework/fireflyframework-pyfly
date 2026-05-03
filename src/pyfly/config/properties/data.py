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
"""Relational data subsystem configuration properties."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.data.relational")
@dataclass
class RelationalProperties:
    """Configuration for the relational data subsystem (pyfly.data.relational.*)."""

    enabled: bool = False
    url: str = "sqlite+aiosqlite:///pyfly.db"
    echo: bool = False
    pool_size: int = 5
