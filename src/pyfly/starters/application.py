# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Application starter — the application-tier microservice bundle.

Mirrors :code:`org.fireflyframework.starter.application` (Java) and
:code:`FireflyFramework.Starter.Application` (.NET). Includes the full
core stack (web, observability, cache, EDA, CQRS, resilience, actuator,
AOP) plus the application-tier extras: plugin registry, security
(JWT + password encoder), scheduling, sessions, i18n, and the
WebSocket session registry. Adds the orchestration engine (saga +
workflow + TCC), idp, callbacks, webhooks, and notifications because
real application services almost always need them; remove the matching
property keys in your ``pyfly.yaml`` to opt out.

Usage — declarative::

    from pyfly.core import pyfly_application
    from pyfly.starters.application import enable_application_stack

    @enable_application_stack
    @pyfly_application(name="my-app", scan_packages=["my_app"])
    class Application:
        pass

Usage — imperative (parity with .NET ``services.AddFireflyApplication``)::

    from pyfly.starters.application import register_application_stack

    app = PyFlyApplication(MyApp)
    register_application_stack(app)
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

#: Properties activated by the application starter — extends the core stack.
APPLICATION_STACK_PROPERTIES: dict[str, str] = {
    **CORE_STACK_PROPERTIES,
    # Plugin SPI (extension points / extensions / dependency-ordered lifecycle)
    "pyfly.plugins.enabled": "true",
    # Security — JWT service + password encoder beans (JwtAutoConfiguration /
    # PasswordEncoderAutoConfiguration gate on pyfly.security.enabled). The auth
    # WebFilter stays opt-in via pyfly.security.jwt.filter.enabled (secure-by-
    # default: the services are wired, request enforcement is opted into).
    "pyfly.security.enabled": "true",
    # Sessions
    "pyfly.session.enabled": "true",
    # i18n — locale resolver + message source
    "pyfly.i18n.enabled": "true",
    # Scheduling — cron + fixed-rate
    "pyfly.scheduling.enabled": "true",
    # Distributed-transaction engine — Saga + Workflow + TCC
    "pyfly.transactional.enabled": "true",
    # Identity provider port + adapter wiring
    "pyfly.idp.enabled": "true",
    # Outbound + inbound webhooks and multi-channel notifications
    "pyfly.callbacks.enabled": "true",
    "pyfly.webhooks.enabled": "true",
    "pyfly.notifications.enabled": "true",
}


def enable_application_stack(cls: type) -> type:
    """Mark *cls* as an application-tier service.

    Setting ``__pyfly_starter_application__`` causes
    :class:`PyFlyApplication` to merge
    :data:`APPLICATION_STACK_PROPERTIES` into the active config.
    """
    cls.__pyfly_starter_application__ = APPLICATION_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls


def register_application_stack(app: PyFlyApplication) -> PyFlyApplication:
    """Imperative API — parity with .NET ``services.AddFireflyApplication``."""
    from pyfly.core.config import Config

    defaults: dict[str, object] = {}
    for dotted_key, raw_value in APPLICATION_STACK_PROPERTIES.items():
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
    "APPLICATION_STACK_PROPERTIES",
    "Autowired",
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
    "enable_application_stack",
    "enable_core_stack",
    "pyfly_application",
    "query_handler",
    "register_application_stack",
    "rest_controller",
    "service",
]
