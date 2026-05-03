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
"""Loggers actuator endpoint — list loggers and change levels at runtime."""

from __future__ import annotations

import logging
from typing import Any


class LoggersEndpoint:
    """Exposes logger configuration at ``/actuator/loggers``.

    GET: Lists all loggers with their effective levels.
    POST (via ``set_logger_level``): Changes a logger's level at runtime.
    """

    @property
    def endpoint_id(self) -> str:
        return "loggers"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        """Return all registered loggers and their levels."""
        manager = logging.Logger.manager
        loggers: dict[str, Any] = {}

        # Root logger
        root = logging.getLogger()
        loggers["ROOT"] = {
            "configuredLevel": logging.getLevelName(root.level),
            "effectiveLevel": logging.getLevelName(root.getEffectiveLevel()),
        }

        # Named loggers
        for name in sorted(manager.loggerDict):
            logger_obj = manager.loggerDict[name]
            if isinstance(logger_obj, logging.Logger):
                loggers[name] = {
                    "configuredLevel": (
                        logging.getLevelName(logger_obj.level) if logger_obj.level != logging.NOTSET else None
                    ),
                    "effectiveLevel": logging.getLevelName(logger_obj.getEffectiveLevel()),
                }
            else:
                # PlaceHolder — only effective level is meaningful
                loggers[name] = {
                    "configuredLevel": None,
                    "effectiveLevel": None,
                }

        return {"loggers": loggers, "levels": ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "OFF"]}

    async def set_logger_level(self, logger_name: str, level: str) -> dict[str, str]:
        """Change a logger's level at runtime.

        Args:
            logger_name: Logger name (use "ROOT" for the root logger).
            level: New level string (e.g. "DEBUG", "INFO").

        Returns:
            Confirmation dict with logger name and new level.
        """
        target = logging.getLogger() if logger_name == "ROOT" else logging.getLogger(logger_name)
        numeric = getattr(logging, level.upper(), None)
        if numeric is None:
            return {"error": f"Unknown level: {level}"}
        target.setLevel(numeric)
        return {"logger": logger_name, "configuredLevel": level.upper()}
