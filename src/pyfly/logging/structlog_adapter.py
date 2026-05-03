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
"""StructlogAdapter — default LoggingPort implementation using structlog."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from pyfly.core.config import Config


class StructlogAdapter:
    """Default logging adapter backed by structlog."""

    def __init__(self) -> None:
        self._root_level: str = "INFO"
        self._format: str = "console"
        self._module_levels: dict[str, str] = {}

    def configure(self, config: Config) -> None:
        """Configure structlog from the logging section of config."""
        level_section = dict(config.get_section("pyfly.logging.level"))
        self._root_level = str(level_section.pop("root", "INFO")).upper()
        self._module_levels = {k: str(v).upper() for k, v in level_section.items()}
        self._format = str(config.get("pyfly.logging.format", "console")).lower()

        self._setup_structlog()
        self._apply_levels()

    def get_logger(self, name: str) -> Any:
        """Get a structlog BoundLogger by name."""
        return structlog.get_logger(name)

    def set_level(self, name: str, level: str) -> None:
        """Set the log level for a specific stdlib logger."""
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger(name).setLevel(log_level)

    def _setup_structlog(self) -> None:
        """Configure structlog processors and stdlib logging."""
        log_level = getattr(logging, self._root_level.upper(), logging.INFO)

        processors: list[structlog.types.Processor] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
        ]

        if self._format == "json":
            processors.append(structlog.processors.JSONRenderer())
        else:
            processors.append(structlog.dev.ConsoleRenderer(sort_keys=False))

        structlog.configure(
            processors=processors,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        logging.basicConfig(
            format="%(message)s",
            stream=sys.stdout,
            level=log_level,
            force=True,
        )

    def _apply_levels(self) -> None:
        """Apply per-module log levels."""
        for module, level in self._module_levels.items():
            self.set_level(module, level)
