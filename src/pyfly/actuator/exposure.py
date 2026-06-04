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
"""Actuator web-exposure model — Spring Boot's ``management.endpoints.web.exposure``.

pyfly mirrors Spring Boot's secure-by-default exposure: only ``health`` and
``info`` are web-exposed unless explicitly included. Configured (under the
pyfly namespace) via:

    pyfly.management.endpoints.web.exposure.include: "health,info"   # or "*"
    pyfly.management.endpoints.web.exposure.exclude: ""
    pyfly.management.endpoints.web.base-path: "/actuator"
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyfly.core.config import Config

# Spring Boot's default web exposure.
DEFAULT_INCLUDE = "health,info"
DEFAULT_BASE_PATH = "/actuator"


def _split(raw: object) -> set[str]:
    """Parse a comma-separated string (or list) of endpoint ids into a set."""
    if isinstance(raw, (list, tuple, set)):
        items: list[str] = [str(i) for i in raw]
    else:
        items = str(raw).split(",")
    return {i.strip() for i in items if i and str(i).strip()}


def web_exposure(config: Config | None) -> tuple[set[str], set[str]]:
    """Return ``(include, exclude)`` endpoint-id sets from config (Spring defaults)."""
    if config is None:
        return _split(DEFAULT_INCLUDE), set()
    include = _split(config.get("pyfly.management.endpoints.web.exposure.include", DEFAULT_INCLUDE))
    exclude = _split(config.get("pyfly.management.endpoints.web.exposure.exclude", ""))
    return include, exclude


def is_web_exposed(endpoint_id: str, include: set[str], exclude: set[str]) -> bool:
    """Return True if *endpoint_id* should be exposed over HTTP.

    ``exclude`` always wins; ``"*"`` in *include* exposes everything else.
    """
    if endpoint_id in exclude:
        return False
    if "*" in include:
        return True
    return endpoint_id in include


def base_path(config: Config | None) -> str:
    """Return the actuator base path (default ``/actuator``)."""
    if config is None:
        return DEFAULT_BASE_PATH
    raw = str(config.get("pyfly.management.endpoints.web.base-path", DEFAULT_BASE_PATH)).strip()
    if not raw:
        return DEFAULT_BASE_PATH
    return "/" + raw.strip("/") if raw != "/" else "/"
