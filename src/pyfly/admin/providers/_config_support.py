# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Shared helpers for the admin Environment + Configuration providers.

These adapt the :class:`~pyfly.core.config.Config` rich API (effective values,
ordered property sources, secret masking) for the admin views, while degrading
gracefully when handed a stub/mock config (returns ``to_dict`` data, no sources).
"""

from __future__ import annotations

import os
from typing import Any

_SCALAR = (str, int, float, bool)


def effective_dict(config: Any) -> dict[str, Any]:
    """Resolved config tree (placeholders + env overrides), or raw fallback."""
    fn = getattr(config, "effective_dict", None)
    if callable(fn):
        result = fn()
        if isinstance(result, dict):
            return result
    raw = config.to_dict() if hasattr(config, "to_dict") else {}
    return raw if isinstance(raw, dict) else {}


def property_sources(config: Any) -> list[dict[str, Any]]:
    """Ordered property sources (highest precedence first), or empty."""
    fn = getattr(config, "property_sources", None)
    if callable(fn):
        result = fn()
        if isinstance(result, list):
            return result
    return []


def mask(config: Any, key: str, value: Any) -> Any:
    """Mask a sensitive value via Config, tolerating stub configs."""
    fn = getattr(config, "mask_value", None)
    if callable(fn):
        result = fn(key, value)
        if result is None or isinstance(result, (*_SCALAR, list)):
            return result
    return value


def is_sensitive(config: Any, key: str) -> bool:
    """True if *key* is treated as a secret by the config's masking rules."""
    return bool(mask(config, key, "_probe_") == "******")


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict to dotted leaf keys (lists kept as values)."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, full))
        else:
            out[full] = value
    return out


def winning_origins(sources: list[dict[str, Any]]) -> dict[str, str]:
    """Map each property key to the highest-precedence source that defines it."""
    origins: dict[str, str] = {}
    for source in sources:
        name = source.get("name", "")
        for key in source.get("properties", {}):
            origins.setdefault(key, name)
    return origins


def effective_origins(config: Any, keys: list[str], sources: list[dict[str, Any]]) -> dict[str, str]:
    """Attribute each *effective* dotted key to its winning source.

    Environment overrides (which appear in the systemEnvironment source under the
    ``PYFLY_*`` name, not the dotted key) take precedence; otherwise the
    highest-precedence file source that defines the dotted key wins.
    """
    file_origins = winning_origins(sources)
    env_key_fn = getattr(config, "_env_key", None)
    origins: dict[str, str] = {}
    for key in keys:
        if callable(env_key_fn):
            env_name = env_key_fn(key)
            if isinstance(env_name, str) and os.environ.get(env_name) is not None:
                origins[key] = "systemEnvironment"
                continue
        origins[key] = file_origins.get(key, "")
    return origins


def group_prefix(dotted_key: str) -> str:
    """Group key by its first two dotted segments (e.g. ``pyfly.server.port`` ->
    ``pyfly.server``); single-segment keys group under themselves."""
    parts = dotted_key.split(".")
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{parts[0]}.{parts[1]}"
