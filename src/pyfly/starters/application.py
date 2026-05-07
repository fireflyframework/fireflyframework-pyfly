# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Application starter — core stack + orchestration + IDP + security."""

from __future__ import annotations

from pyfly.starters.core import CORE_STACK_PROPERTIES

APPLICATION_STACK_PROPERTIES: dict[str, str] = {
    **CORE_STACK_PROPERTIES,
    "pyfly.transactional.enabled": "true",
    "pyfly.idp.enabled": "true",
    "pyfly.security-jwt.enabled": "true",
    "pyfly.security-password.enabled": "true",
    "pyfly.session.enabled": "true",
    "pyfly.session-filter.enabled": "true",
    "pyfly.callbacks.enabled": "true",
    "pyfly.webhooks.enabled": "true",
    "pyfly.notifications.enabled": "true",
}


def enable_application_stack(cls: type) -> type:
    cls.__pyfly_starter_application__ = APPLICATION_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls
