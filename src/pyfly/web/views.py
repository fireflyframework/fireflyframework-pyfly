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
"""Explicit framework-independent HTML and redirect results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelAndView:
    template: str
    model: Mapping[str, Any] = field(default_factory=dict)
    status_code: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Redirect:
    location: str
    status_code: int = 303
    allow_external: bool = False

    def __post_init__(self) -> None:
        location = self.location
        parsed = urlsplit(location)
        if any(ord(c) < 32 for c in location) or "\\" in location:
            raise ValueError("Invalid redirect location")
        if self.status_code not in (301, 302, 303, 307, 308):
            raise ValueError("Invalid redirect status")
        if location.startswith("//") or (parsed.scheme and parsed.scheme not in ("http", "https")):
            raise ValueError("Invalid redirect scheme")
        if not self.allow_external and (parsed.netloc or parsed.scheme or not location.startswith("/")):
            raise ValueError("Redirect must be an application-local path")
