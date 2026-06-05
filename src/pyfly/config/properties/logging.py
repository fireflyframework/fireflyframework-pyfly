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
"""Logging subsystem configuration properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.core.config import config_properties


@dataclass
class PatternProperties:
    """Custom log layout patterns (logback-style tokens)."""

    console: str = ""
    file: str = ""


@dataclass
class FileProperties:
    """File appender — set ``name`` to enable file output (console stays on)."""

    name: str = ""
    path: str = ""


@dataclass
class RollingProperties:
    """Rotation policy for the file appender."""

    max_size: str = "10MB"
    max_history: int = 7
    total_size_cap: str = ""


@dataclass
class StreamsRedactionProperties:
    """Opt-in stdout/stderr redaction wrapper."""

    enabled: bool = False


@dataclass
class PresidioProperties:
    """Microsoft Presidio engine settings (used when engine=presidio|auto)."""

    languages: list[str] = field(default_factory=lambda: ["en"])
    score_threshold: float = 0.5


@dataclass
class RedactionProperties:
    """PII redaction settings (pyfly.logging.redaction.*)."""

    enabled: bool = True
    engine: str = "auto"  # regex | presidio | auto
    entities: list[str] = field(
        default_factory=lambda: [
            "EMAIL",
            "CREDIT_CARD",
            "IBAN",
            "US_SSN",
            "JWT",
            "BEARER_TOKEN",
            "URL_CREDENTIALS",
            "PHONE",
        ]
    )
    mask: str = "placeholder"  # placeholder | partial | hash
    extra_patterns: dict[str, str] = field(default_factory=dict)
    allow_fields: list[str] = field(default_factory=list)
    deny_fields: list[str] = field(default_factory=lambda: ["password", "token", "secret"])
    streams: StreamsRedactionProperties = field(default_factory=StreamsRedactionProperties)
    presidio: PresidioProperties = field(default_factory=PresidioProperties)


@config_properties(prefix="pyfly.logging")
@dataclass
class LoggingProperties:
    """Configuration for the logging subsystem (pyfly.logging.*)."""

    level: dict[str, Any] = field(default_factory=lambda: {"root": "INFO"})
    format: str = "console"  # console | json | logfmt
    pattern: PatternProperties = field(default_factory=PatternProperties)
    file: FileProperties = field(default_factory=FileProperties)
    rolling: RollingProperties = field(default_factory=RollingProperties)
    config: str = ""  # external dictConfig YAML / fileConfig INI path
    redaction: RedactionProperties = field(default_factory=RedactionProperties)
