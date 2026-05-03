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
"""StdlibLoggingAdapter — zero-dependency LoggingPort fallback using stdlib logging."""

from __future__ import annotations

import logging
import sys
from typing import Any

from pyfly.core.config import Config


class _StructuredLogger:
    """Wraps stdlib Logger to accept structlog-style calls: logger.info(event, **kwargs)."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _format(self, event: str, kwargs: dict[str, Any]) -> str:
        if kwargs:
            pairs = " ".join(f"{k}={v}" for k, v in kwargs.items())
            return f"{event} | {pairs}"
        return event

    def debug(self, event: str, **kwargs: Any) -> None:
        self._logger.debug(self._format(event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self._logger.info(self._format(event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self._logger.warning(self._format(event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self._logger.error(self._format(event, kwargs))

    def critical(self, event: str, **kwargs: Any) -> None:
        self._logger.critical(self._format(event, kwargs))

    def exception(self, event: str, **kwargs: Any) -> None:
        self._logger.exception(self._format(event, kwargs))


class StdlibLoggingAdapter:
    """Fallback LoggingPort using only stdlib logging.

    Used when ``structlog`` is not installed. Provides structured-style
    log output (``event | key=value``) through the standard library.
    """

    def __init__(self) -> None:
        self._root_level: str = "INFO"
        self._format: str = "console"
        self._module_levels: dict[str, str] = {}

    def configure(self, config: Config) -> None:
        """Configure stdlib logging from the logging section of config."""
        level_section = dict(config.get_section("pyfly.logging.level"))
        self._root_level = str(level_section.pop("root", "INFO")).upper()
        self._module_levels = {k: str(v).upper() for k, v in level_section.items()}
        self._format = str(config.get("pyfly.logging.format", "console")).lower()

        self._setup_logging()
        self._apply_levels()

    def get_logger(self, name: str) -> Any:
        """Get a structured logger backed by stdlib logging."""
        return _StructuredLogger(logging.getLogger(name))

    def set_level(self, name: str, level: str) -> None:
        """Set the log level for a specific stdlib logger."""
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger(name).setLevel(log_level)

    def _setup_logging(self) -> None:
        """Configure stdlib logging with a sensible format."""
        log_level = getattr(logging, self._root_level.upper(), logging.INFO)

        if self._format == "json":
            fmt = '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
        else:
            fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

        logging.basicConfig(
            format=fmt,
            stream=sys.stdout,
            level=log_level,
            force=True,
        )

    def _apply_levels(self) -> None:
        """Apply per-module log levels."""
        for module, level in self._module_levels.items():
            self.set_level(module, level)
