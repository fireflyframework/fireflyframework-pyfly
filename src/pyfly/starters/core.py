# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Core starter — web + observability + cache + EDA + CQRS + resilience.

Mirrors ``org.fireflyframework.starter.core``: every infrastructure-tier
service should sit on top of this stack.
"""

from __future__ import annotations

#: Property keys this starter activates at boot.
CORE_STACK_PROPERTIES: dict[str, str] = {
    "pyfly.web.enabled": "true",
    "pyfly.observability.enabled": "true",
    "pyfly.cache.enabled": "true",
    "pyfly.eda.enabled": "true",
    "pyfly.cqrs.enabled": "true",
    "pyfly.resilience.enabled": "true",
    "pyfly.actuator.enabled": "true",
    "pyfly.actuator.metrics.enabled": "true",
}


def enable_core_stack(cls: type) -> type:
    """Decorator marker — the bootstrapper consults this to activate the stack."""
    cls.__pyfly_starter_core__ = CORE_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls
