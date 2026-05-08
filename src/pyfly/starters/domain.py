# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Domain starter — core stack + DDD building blocks + event sourcing + CQRS + transactional engine.

Mirrors :code:`org.fireflyframework.starter.domain` (Java) and
:code:`FireflyFramework.Starter.Domain` (.NET) — the bundle a
*domain-tier* microservice typically depends on. Importing from this
module also re-exports the :mod:`pyfly.domain` DDD primitives
(:class:`Entity`, :class:`ValueObject`, :class:`AggregateRoot`,
:class:`DomainEvent`, :class:`Specification`, :class:`DomainRepository`,
:class:`DomainException`, :class:`BusinessRuleViolation`,
:class:`AggregateNotFound`) so a single ``from pyfly.starters.domain
import ...`` brings everything a domain microservice needs.
"""

from __future__ import annotations

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
from pyfly.starters.core import CORE_STACK_PROPERTIES

DOMAIN_STACK_PROPERTIES: dict[str, str] = {
    **CORE_STACK_PROPERTIES,
    "pyfly.eventsourcing.enabled": "true",
    "pyfly.transactional.enabled": "true",
    "pyfly.rule-engine.enabled": "true",
    "pyfly.relational.enabled": "true",
}


def enable_domain_stack(cls: type) -> type:
    """Mark *cls* as a domain-tier application.

    The framework's bootstrapper consults
    ``__pyfly_starter_domain__`` to activate every property in
    :data:`DOMAIN_STACK_PROPERTIES` at boot.
    """
    cls.__pyfly_starter_domain__ = DOMAIN_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls


__all__ = [
    "DOMAIN_STACK_PROPERTIES",
    "enable_domain_stack",
    # DDD primitives — re-exported for one-stop import
    "AggregateNotFound",
    "AggregateRoot",
    "BusinessRuleViolation",
    "DomainEvent",
    "DomainException",
    "DomainRepository",
    "Entity",
    "Specification",
    "ValueObject",
]
