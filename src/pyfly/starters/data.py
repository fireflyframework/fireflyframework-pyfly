# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Data starter — the data-tier microservice bundle.

Mirrors :code:`org.fireflyframework.starter.data` (Java) and
:code:`FireflyFramework.Starter.Data` (.NET). Activates the core stack
plus the relational (SQLAlchemy) and document (MongoDB / Beanie)
adapters, the HTTP client (httpx), and scheduling for periodic
ingestion / enrichment jobs.

Usage — declarative::

    from pyfly.core import pyfly_application
    from pyfly.starters.data import enable_data_stack

    @enable_data_stack
    @pyfly_application(name="my-data-job", scan_packages=["my_job"])
    class Application:
        pass

Usage — imperative (parity with .NET ``services.AddFireflyData``)::

    from pyfly.starters.data import register_data_stack

    app = PyFlyApplication(MyApp)
    register_data_stack(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.starters.core import (
    CORE_STACK_PROPERTIES,
    Autowired,
    Command,
    CommandBus,
    CommandHandler,
    Query,
    QueryBus,
    QueryHandler,
    Scope,
    command_handler,
    component,
    configuration,
    enable_core_stack,
    pyfly_application,
    query_handler,
    rest_controller,
    service,
)

if TYPE_CHECKING:
    from pyfly.core.application import PyFlyApplication

#: Properties activated by the data starter — extends the core stack.
DATA_STACK_PROPERTIES: dict[str, str] = {
    **CORE_STACK_PROPERTIES,
    # Relational data — SQLAlchemy adapter, repositories, transactions
    "pyfly.data.relational.enabled": "true",
    # Document data — MongoDB / Beanie ODM
    "pyfly.data.document.enabled": "true",
    # Outbound HTTP client — for fetching from upstream feeds
    "pyfly.client.enabled": "true",
    # Scheduling — cron / fixed-rate jobs
    "pyfly.scheduling.enabled": "true",
    # Resilience — retry, circuit breaker, bulkhead in front of external calls
    "pyfly.resilience.enabled": "true",
}


def enable_data_stack(cls: type) -> type:
    """Mark *cls* as a data-tier application."""
    cls.__pyfly_starter_data__ = DATA_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls


def register_data_stack(app: PyFlyApplication) -> PyFlyApplication:
    """Imperative API — parity with .NET ``services.AddFireflyData``."""
    from pyfly.core.config import Config

    defaults: dict[str, object] = {}
    for dotted_key, raw_value in DATA_STACK_PROPERTIES.items():
        _merge_dotted(defaults, dotted_key, raw_value)
    app.config._data = Config._deep_merge(app.config._data, defaults)
    return app


def _merge_dotted(target: dict[str, object], key: str, value: object) -> None:
    parts = key.split(".")
    cursor: dict[str, object] = target
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[parts[-1]] = value


__all__ = [
    "Autowired",
    "Command",
    "CommandBus",
    "CommandHandler",
    "DATA_STACK_PROPERTIES",
    "Query",
    "QueryBus",
    "QueryHandler",
    "Scope",
    "command_handler",
    "component",
    "configuration",
    "enable_core_stack",
    "enable_data_stack",
    "pyfly_application",
    "query_handler",
    "register_data_stack",
    "rest_controller",
    "service",
]
