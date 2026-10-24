# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Lightweight entry point; optional AI dependencies are checked only when enabled."""

from __future__ import annotations

import importlib.util
import json
import os
from typing import TYPE_CHECKING, Any

from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config

if TYPE_CHECKING:
    from pyfly.agentic.config import AgenticConfig


def bind_agentic_config(config: Config) -> AgenticConfig:
    from pyfly.agentic.config import REMOVED_NATIVE_FIELDS, AgenticConfig

    if "FIREFLY_DBSTORE_CACHE_ROOT" in os.environ:
        raise ValueError(
            "FIREFLY_DBSTORE_CACHE_ROOT was removed; use PYFLY_AGENTIC_ARTIFACT_CACHE_ROOT, "
            "preserving the existing path"
        )
    raw_section = config.get("pyfly.agentic", {})
    if isinstance(raw_section, dict):
        for key in raw_section:
            normalized = key.replace("-", "_")
            if normalized in REMOVED_NATIVE_FIELDS:
                raise ValueError(f"{key} was removed; {REMOVED_NATIVE_FIELDS[normalized]}")
    for key, guidance in REMOVED_NATIVE_FIELDS.items():
        if "PYFLY_AGENTIC_" + key.upper() in os.environ:
            raise ValueError(f"{key} was removed; {guidance}")
    bound = config.bind(AgenticConfig)
    values = bound.model_dump()
    for field in AgenticConfig.model_fields:
        raw = os.environ.get("PYFLY_AGENTIC_" + field.upper())
        if raw is not None:
            try:
                values[field] = json.loads(raw)
            except json.JSONDecodeError:
                values[field] = raw
    return AgenticConfig.model_validate(values)


def require_agentic_dependencies() -> None:
    missing = [
        name
        for name in ("pydantic_ai", "pydantic_graph", "jinja2", "genai_prices")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise RuntimeError("pyfly.agentic.enabled requires pyfly[agentic]; missing: " + ", ".join(missing))


@auto_configuration
@conditional_on_property("pyfly.agentic.enabled", having_value="true")
class AgenticAutoConfiguration:
    def __new__(cls) -> Any:
        require_agentic_dependencies()
        from pyfly.agentic._native_auto_configuration import NativeAgenticAutoConfiguration

        return NativeAgenticAutoConfiguration()
