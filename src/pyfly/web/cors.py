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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.core.config import Config

# Spring's CorsConfiguration.applyPermitDefaultValues() default method set,
# applied when CORS is enabled but no methods are configured explicitly.
_PERMIT_DEFAULT_METHODS = ["GET", "HEAD", "POST"]


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _as_list(value: Any) -> list[str] | None:
    """Normalize a YAML list or comma-separated string into a list of strings."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return None
    return [part.strip() for part in text.split(",") if part.strip()]


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

    @classmethod
    def from_config(cls, config: Config) -> CORSConfig | None:
        """Build a CORSConfig from ``pyfly.web.cors.*`` properties.

        Returns ``None`` when CORS is not enabled (``pyfly.web.cors.enabled``
        is falsey), mirroring Spring's ``CorsAutoConfiguration`` which only
        registers the CORS filter when the user opts in via configuration.
        Both kebab-case (``allowed-origins``) and snake_case (``allowed_origins``)
        keys are accepted.
        """
        if not _as_bool(config.get("pyfly.web.cors.enabled", False)):
            return None

        def _read_list(name: str) -> list[str] | None:
            raw = config.get(f"pyfly.web.cors.{name}")
            if raw is None:
                raw = config.get(f"pyfly.web.cors.{name.replace('-', '_')}")
            return _as_list(raw)

        origins = _read_list("allowed-origins") or ["*"]
        methods = _read_list("allowed-methods") or list(_PERMIT_DEFAULT_METHODS)
        headers = _read_list("allowed-headers") or ["*"]
        exposed = _read_list("exposed-headers") or []
        credentials = _as_bool(config.get("pyfly.web.cors.allow-credentials", False))
        max_age = int(config.get("pyfly.web.cors.max-age", 600) or 600)

        return cls(
            allowed_origins=origins,
            allowed_methods=methods,
            allowed_headers=headers,
            allow_credentials=credentials,
            exposed_headers=exposed,
            max_age=max_age,
        )
