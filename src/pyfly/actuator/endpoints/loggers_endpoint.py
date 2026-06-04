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
"""Loggers actuator endpoint — Spring Boot ``/actuator/loggers`` parity.

Reports and configures logger levels using Spring's level vocabulary
(``OFF/ERROR/WARN/INFO/DEBUG/TRACE``), not Python's (``CRITICAL/WARNING/NOTSET``),
so the response is drop-in compatible with Spring Boot tooling and Spring Boot
Admin.
"""

from __future__ import annotations

import logging
from typing import Any

# Spring Boot's level vocabulary (most→least severe, plus OFF).
SPRING_LEVELS = ["OFF", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"]

# Python has no TRACE/OFF; map to sentinel numeric levels.
_TRACE = 5
_OFF = 1000
_SPRING_TO_PY = {
    "OFF": _OFF,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "TRACE": _TRACE,
}

# Built-in logger groups, mirroring Spring Boot's defaults.
_DEFAULT_GROUPS: dict[str, list[str]] = {
    "web": ["pyfly.web", "uvicorn", "starlette"],
    "sql": ["sqlalchemy", "pyfly.data"],
}


def _py_to_spring(level: int) -> str | None:
    """Map a Python numeric level to Spring's level name (None when unset)."""
    if level == logging.NOTSET:
        return None
    if level >= _OFF:
        return "OFF"
    if level >= logging.ERROR:  # ERROR (40) and CRITICAL (50) collapse to ERROR
        return "ERROR"
    if level >= logging.WARNING:
        return "WARN"
    if level >= logging.INFO:
        return "INFO"
    if level >= logging.DEBUG:
        return "DEBUG"
    return "TRACE"


class LoggersEndpoint:
    """Exposes logger configuration at ``/actuator/loggers``.

    GET /actuator/loggers          -> levels + loggers + groups
    GET /actuator/loggers/{name}   -> {configuredLevel, effectiveLevel}
    POST /actuator/loggers/{name}  -> set/reset a level (handled by the adapter)
    """

    @property
    def endpoint_id(self) -> str:
        return "loggers"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        """Return all registered loggers, their levels, and logger groups."""
        manager = logging.Logger.manager
        loggers: dict[str, Any] = {}

        root = logging.getLogger()
        loggers["ROOT"] = {
            "configuredLevel": _py_to_spring(root.level),
            "effectiveLevel": _py_to_spring(root.getEffectiveLevel()),
        }

        for name in sorted(manager.loggerDict):
            logger_obj = manager.loggerDict[name]
            if isinstance(logger_obj, logging.Logger):
                loggers[name] = {
                    "configuredLevel": _py_to_spring(logger_obj.level),
                    "effectiveLevel": _py_to_spring(logger_obj.getEffectiveLevel()),
                }
            else:
                # PlaceHolder — no own level.
                loggers[name] = {"configuredLevel": None, "effectiveLevel": None}

        groups = {group: {"configuredLevel": None, "members": members} for group, members in _DEFAULT_GROUPS.items()}

        return {"levels": SPRING_LEVELS, "loggers": loggers, "groups": groups}

    async def get_logger(self, name: str) -> dict[str, Any]:
        """Return a single logger's configured + effective level (Spring shape)."""
        target = self._resolve(name)
        return {
            "configuredLevel": _py_to_spring(target.level),
            "effectiveLevel": _py_to_spring(target.getEffectiveLevel()),
        }

    async def set_logger_level(self, name: str, level: str | None) -> dict[str, str] | None:
        """Set or reset a logger level. ``level=None`` resets to inherited (NOTSET).

        Returns ``None`` on success (HTTP 204) or an ``{"error": ...}`` dict on a
        bad level (HTTP 400)."""
        target = self._resolve(name)
        if level is None or level == "":
            target.setLevel(logging.NOTSET)
            return None
        spring = str(level).upper()
        if spring not in _SPRING_TO_PY:
            return {"error": f"Unknown level: {level}. Valid levels: {', '.join(SPRING_LEVELS)}"}
        target.setLevel(_SPRING_TO_PY[spring])
        return None

    @staticmethod
    def _resolve(name: str) -> logging.Logger:
        return logging.getLogger() if name.upper() == "ROOT" else logging.getLogger(name)
