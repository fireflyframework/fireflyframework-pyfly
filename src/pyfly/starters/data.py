# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Data starter — core stack + relational/document data + full resilience."""

from __future__ import annotations

from pyfly.starters.core import CORE_STACK_PROPERTIES

DATA_STACK_PROPERTIES: dict[str, str] = {
    **CORE_STACK_PROPERTIES,
    "pyfly.relational.enabled": "true",
    "pyfly.document.enabled": "true",
    "pyfly.client.enabled": "true",
    "pyfly.scheduling.enabled": "true",
}


def enable_data_stack(cls: type) -> type:
    cls.__pyfly_starter_data__ = DATA_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls
