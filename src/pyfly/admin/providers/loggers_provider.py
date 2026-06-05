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
"""Loggers data provider — logger listing and level management."""

from __future__ import annotations

import logging
from typing import Any

# Spring-style levels Python's logging module has no direct constant for.
# TRACE maps below DEBUG (most verbose); OFF disables the logger entirely.
_EXTRA_LEVELS: dict[str, int] = {
    "TRACE": 5,
    "OFF": logging.CRITICAL + 1,
}

_LOGGER_DESCRIPTIONS: dict[str, str] = {
    "pyfly.core": "Framework core (bootstrap, lifecycle)",
    "pyfly.web": "Web layer (HTTP, routing, filters)",
    "pyfly.container": "IoC container (bean resolution, injection)",
    "pyfly.security": "Security (authentication, authorization)",
    "pyfly.admin": "Admin dashboard",
    "pyfly.data": "Data access layer",
    "pyfly.cache": "Caching subsystem",
    "pyfly.cqrs": "CQRS (commands, queries, buses)",
    "pyfly.scheduling": "Task scheduling",
    "pyfly.messaging": "Messaging (Kafka, RabbitMQ)",
    "pyfly.resilience": "Resilience (circuit breaker, retry)",
    "pyfly.transactional": "Distributed transactions (saga, TCC)",
    "pyfly.observability": "Observability (tracing, metrics)",
    "pyfly.client": "HTTP client",
    "pyfly.config": "Configuration subsystem",
    "pyfly.context": "Application context",
    "uvicorn": "ASGI server (Uvicorn)",
    "granian": "ASGI server (Granian)",
    "hypercorn": "ASGI server (Hypercorn)",
    "structlog": "Structured logging",
    "sqlalchemy": "SQL database toolkit",
    "asyncio": "Python asyncio runtime",
    "httpx": "HTTP client library",
    "fastapi": "FastAPI framework",
    "starlette": "Starlette ASGI framework",
}


class LoggersProvider:
    """Provides logger data and level management."""

    @staticmethod
    def _infer_description(name: str) -> str:
        """Map known logger name prefixes to descriptions."""
        if name == "ROOT":
            return "Root logger (parent of all loggers)"
        for prefix, desc in _LOGGER_DESCRIPTIONS.items():
            if name == prefix or name.startswith(prefix + "."):
                return desc
        return ""

    async def get_loggers(self) -> dict[str, Any]:
        manager = logging.Logger.manager
        loggers: dict[str, Any] = {}

        root = logging.getLogger()
        loggers["ROOT"] = {
            "configuredLevel": logging.getLevelName(root.level),
            "effectiveLevel": logging.getLevelName(root.getEffectiveLevel()),
            "description": self._infer_description("ROOT"),
        }

        for name in sorted(manager.loggerDict):
            logger_obj = manager.loggerDict[name]
            if isinstance(logger_obj, logging.Logger):
                loggers[name] = {
                    "configuredLevel": (
                        logging.getLevelName(logger_obj.level) if logger_obj.level != logging.NOTSET else None
                    ),
                    "effectiveLevel": logging.getLevelName(logger_obj.getEffectiveLevel()),
                    "description": self._infer_description(name),
                }

        return {
            "loggers": loggers,
            "levels": ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        }

    async def set_level(self, logger_name: str, level: str) -> dict[str, str]:
        target = logging.getLogger() if logger_name == "ROOT" else logging.getLogger(logger_name)
        level_name = level.upper()
        # TRACE/OFF are advertised as valid levels but have no logging.<NAME>
        # constant, so resolve them explicitly before falling back (audit #69).
        numeric = _EXTRA_LEVELS.get(level_name)
        if numeric is None:
            numeric = getattr(logging, level_name, None)
        if numeric is None:
            return {"error": f"Unknown level: {level}"}
        target.setLevel(numeric)
        return {"logger": logger_name, "configuredLevel": level_name}
