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
"""Opt-in data administration settings."""

from dataclasses import dataclass, field

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.admin.data")
@dataclass
class AdminDataProperties:
    enabled: bool = False
    allowed_roles: list[str] = field(default_factory=lambda: ["ADMIN"])
    page_size: int = 25
    max_page_size: int = 100
    max_body_size: int = 1024 * 1024
    edit_token_key: str = field(default="", repr=False)
    operations: list[str] = field(default_factory=lambda: ["list", "read", "create", "update", "delete"])

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= self.max_page_size or self.max_page_size > 1000 or self.max_body_size < 1:
            raise ValueError("Invalid admin pagination/body limits")
        if self.enabled and not self.allowed_roles:
            raise ValueError("Data administration requires at least one allowed role")
