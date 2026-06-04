# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Core starter — the foundation every Firefly service is built on.

Mirrors :code:`org.fireflyframework.starter.core` (Java) and
:code:`FireflyFramework.Starter.Core` (.NET). The .NET version registers
``AddFireflyWeb`` / ``AddFireflyObservability`` / ``AddFireflyCache`` /
``AddFireflyEda`` / ``AddFireflyCqrs`` in a single call; the Python
equivalent activates the same modules through the property-driven
auto-configuration mechanism.

Usage — declarative (preferred)::

    from pyfly.core import pyfly_application
    from pyfly.starters.core import enable_core_stack

    @enable_core_stack
    @pyfly_application(name="my-service", scan_packages=["my_service"])
    class Application:
        pass

Usage — imperative (parity with .NET ``services.AddFireflyCore``)::

    from pyfly.starters.core import register_core_stack

    app = PyFlyApplication(MyApp)
    register_core_stack(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-exports — single import line for the layer.
from pyfly.container import (
    Autowired,
    Scope,
    component,
    configuration,
    rest_controller,
    service,
)
from pyfly.core.application import pyfly_application
from pyfly.cqrs import (
    Command,
    CommandBus,
    CommandHandler,
    Query,
    QueryBus,
    QueryHandler,
    command_handler,
    query_handler,
)

if TYPE_CHECKING:
    from pyfly.core.application import PyFlyApplication

#: Property keys this starter activates at boot.
#:
#: The :class:`PyFlyApplication` bootstrap merges this dict into the live
#: configuration so each property-gated ``@auto_configuration`` (CQRS,
#: cache, EDA, observability, actuator, resilience) wires up automatically.
CORE_STACK_PROPERTIES: dict[str, str] = {
    # Web tier — Starlette/FastAPI auto-config + ASGI server selection
    "pyfly.web.enabled": "true",
    "pyfly.server.enabled": "true",
    # Observability — Prometheus + OpenTelemetry
    "pyfly.observability.enabled": "true",
    "pyfly.metrics.enabled": "true",
    "pyfly.tracing.enabled": "true",
    # Caching — Redis if installed, in-memory fallback otherwise
    "pyfly.cache.enabled": "true",
    # Event-Driven Architecture — Kafka/RabbitMQ if installed, in-memory broker
    # otherwise. EdaAutoConfiguration gates on ``pyfly.eda.provider`` (not
    # *.enabled); "auto" resolves to the best installed broker, falling back to
    # the in-memory bus.
    "pyfly.eda.provider": "auto",
    # CQRS — CommandBus / QueryBus + handler discovery
    "pyfly.cqrs.enabled": "true",
    # Resilience — rate limiter, bulkhead, timeout, fallback
    "pyfly.resilience.enabled": "true",
    # Actuator — /health, /info, /metrics (gated on pyfly.web.actuator.enabled)
    "pyfly.web.actuator.enabled": "true",
    # AOP — required by @cacheable, @authorize, etc.
    "pyfly.aop.enabled": "true",
}


def enable_core_stack(cls: type) -> type:
    """Mark *cls* as a core-tier application.

    Setting ``__pyfly_starter_core__`` causes :class:`PyFlyApplication`
    to merge :data:`CORE_STACK_PROPERTIES` into the active config before
    auto-configurations run.
    """
    cls.__pyfly_starter_core__ = CORE_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls


def register_core_stack(app: PyFlyApplication) -> PyFlyApplication:
    """Imperative API — parity with .NET ``services.AddFireflyCore``.

    Useful when you build the :class:`PyFlyApplication` programmatically
    (rather than through the ``@enable_core_stack`` decorator) and still
    want the same property defaults applied. Call before ``app.startup()``.
    """
    from pyfly.core.config import Config

    defaults: dict[str, object] = {}
    for dotted_key, raw_value in CORE_STACK_PROPERTIES.items():
        _merge_dotted(defaults, dotted_key, raw_value)
    # Imperative API is authoritative — explicit call wins over anything
    # already in the config (including a user pyfly.yaml). Mirrors .NET's
    # ``services.AddFireflyCore(...)`` semantics where the last registration
    # wins. Use the ``@enable_core_stack`` decorator instead if you want
    # user pyfly.yaml values to keep winning over the bundle.
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
    "CORE_STACK_PROPERTIES",
    "Command",
    "CommandBus",
    "CommandHandler",
    "Query",
    "QueryBus",
    "QueryHandler",
    "Scope",
    "command_handler",
    "component",
    "configuration",
    "enable_core_stack",
    "pyfly_application",
    "query_handler",
    "register_core_stack",
    "rest_controller",
    "service",
]
