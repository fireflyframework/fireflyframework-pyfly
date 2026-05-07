# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Domain starter — core stack + event sourcing + CQRS + transactional engine."""

from __future__ import annotations

from pyfly.starters.core import CORE_STACK_PROPERTIES

DOMAIN_STACK_PROPERTIES: dict[str, str] = {
    **CORE_STACK_PROPERTIES,
    "pyfly.eventsourcing.enabled": "true",
    "pyfly.transactional.enabled": "true",
    "pyfly.rule-engine.enabled": "true",
    "pyfly.relational.enabled": "true",
}


def enable_domain_stack(cls: type) -> type:
    cls.__pyfly_starter_domain__ = DOMAIN_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls
