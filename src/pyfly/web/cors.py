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
"""CORS configuration for PyFly web applications."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CORSConfig:
    """Configuration for Cross-Origin Resource Sharing.

    Mirrors Spring Boot's CorsConfiguration with sensible defaults.
    """

    allowed_origins: list[str] = field(default_factory=lambda: ["*"])
    allowed_methods: list[str] = field(default_factory=lambda: ["GET"])
    allowed_headers: list[str] = field(default_factory=lambda: ["*"])
    allow_credentials: bool = False
    exposed_headers: list[str] = field(default_factory=list)
    max_age: int = 600  # seconds
