# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Domain starter — the domain-tier microservice bundle.

Mirrors :code:`org.fireflyframework.starter.domain` (Java) and
:code:`FireflyFramework.Starter.Domain` (.NET). Activates the same set
of capabilities the Java starter pulls in:

* WebFlux equivalent — Starlette/FastAPI adapter via the core stack
* Validation — :class:`Valid[T]` annotation with Pydantic
* AOP — required by ``@cacheable`` / ``@authorize`` / ``@retry``
* Spring Retry equivalent — pyfly resilience module (retry / circuit breaker)
* Actuator — health, info, metrics
* Observability — Prometheus + OpenTelemetry
* CQRS — CommandBus / QueryBus + handler discovery
* HTTP client — httpx-based with circuit breakers
* EDA — Kafka / RabbitMQ / in-memory broker
* Orchestration — Saga, Workflow, TCC engines
* Event Sourcing — AggregateRoot, EventStore, snapshots, outbox
* Rule Engine — YAML DSL for externalised business rules
* Plugins — extension-point SPI

In addition, importing from this module re-exports every
:mod:`pyfly.domain` DDD primitive so a single line is enough for a
domain microservice file:

.. code-block:: python

    from pyfly.starters.domain import (
        AggregateRoot, BusinessRuleViolation, DomainEvent,
        DomainRepository, Entity, Specification, ValueObject,
        Command, CommandHandler, command_handler,
        Query, QueryHandler, query_handler,
        rest_controller, service, configuration,
        enable_domain_stack, pyfly_application,
    )

Usage — declarative::

    from pyfly.core import pyfly_application
    from pyfly.starters.domain import enable_domain_stack

    @enable_domain_stack
    @pyfly_application(name="my-domain-service", scan_packages=["my_service"])
    class Application:
        pass

Usage — imperative (parity with .NET ``services.AddFireflyDomain``)::

    from pyfly.starters.domain import register_domain_stack

    app = PyFlyApplication(MyApp)
    register_domain_stack(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# DDD primitives — re-exported for one-stop import.
from pyfly.domain import (
    AggregateNotFound,
    AggregateRoot,
    BusinessRuleViolation,
    DomainEvent,
    DomainException,
    DomainRepository,
    Entity,
    Specification,
    ValueObject,
)
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

#: Properties activated by the domain starter — extends the core stack.
DOMAIN_STACK_PROPERTIES: dict[str, str] = {
    **CORE_STACK_PROPERTIES,
    # Event Sourcing — AggregateRoot, EventStore, snapshots, outbox
    "pyfly.eventsourcing.enabled": "true",
    # Distributed-transaction engine — Saga, Workflow, TCC
    "pyfly.transactional.enabled": "true",
    # Rule engine — YAML DSL for externalised business rules
    "pyfly.rule-engine.enabled": "true",
    # Relational data — domain microservices typically own at least one DB
    "pyfly.relational.enabled": "true",
    # Outbound HTTP client — for cross-service domain calls
    "pyfly.client.enabled": "true",
    # Plugin SPI — domain services often expose extension points
    "pyfly.plugins.enabled": "true",
}


def enable_domain_stack(cls: type) -> type:
    """Mark *cls* as a domain-tier application.

    Setting ``__pyfly_starter_domain__`` causes :class:`PyFlyApplication`
    to merge :data:`DOMAIN_STACK_PROPERTIES` into the active config.
    """
    cls.__pyfly_starter_domain__ = DOMAIN_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls


def register_domain_stack(app: PyFlyApplication) -> PyFlyApplication:
    """Imperative API — parity with .NET ``services.AddFireflyDomain``."""
    from pyfly.core.config import Config

    defaults: dict[str, object] = {}
    for dotted_key, raw_value in DOMAIN_STACK_PROPERTIES.items():
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
    "AggregateNotFound",
    "AggregateRoot",
    "Autowired",
    "BusinessRuleViolation",
    "Command",
    "CommandBus",
    "CommandHandler",
    "DOMAIN_STACK_PROPERTIES",
    "DomainEvent",
    "DomainException",
    "DomainRepository",
    "Entity",
    "Query",
    "QueryBus",
    "QueryHandler",
    "Scope",
    "Specification",
    "ValueObject",
    "command_handler",
    "component",
    "configuration",
    "enable_core_stack",
    "enable_domain_stack",
    "pyfly_application",
    "query_handler",
    "register_domain_stack",
    "rest_controller",
    "service",
]
