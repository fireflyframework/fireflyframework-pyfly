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
"""Auto-configuration utilities and discovery (inspired by Spring Boot auto-config)."""

from __future__ import annotations

import importlib
from typing import Any


class AutoConfiguration:
    """Shared utility for checking importable packages.

    Provider detection (``detect_provider()``) lives in each subsystem's own
    ``auto_configuration`` module — **not** here. This class only exposes the
    generic ``is_available()`` helper so subsystems can probe for optional
    dependencies without duplicating the ``importlib`` boilerplate.
    """

    @staticmethod
    def is_available(module_name: str) -> bool:
        """Check if a Python package is importable."""
        try:
            importlib.import_module(module_name)
            return True
        except ImportError:
            return False


def discover_auto_configurations() -> list[type]:
    """Discover @auto_configuration classes via ``pyfly.auto_configuration`` entry points.

    Each subsystem registers its auto-configuration class as an entry point
    in ``pyproject.toml`` under the ``[project.entry-points."pyfly.auto_configuration"]``
    group. Third-party packages can add their own auto-configuration classes
    by declaring entries in the same group.

    This mirrors Spring Boot's ``META-INF/spring.factories`` / ``AutoConfiguration.imports``
    mechanism — fully pluggable, no hardcoded imports.
    """
    from importlib.metadata import entry_points

    classes: list[type] = []
    for ep in entry_points(group="pyfly.auto_configuration"):
        try:
            cls = ep.load()
            if getattr(cls, "__pyfly_auto_configuration__", False):
                classes.append(cls)
        except ImportError:
            import logging

            logging.getLogger(__name__).debug(
                "Skipped auto-configuration entry point '%s': %s",
                ep.name,
                ep.value,
            )
    return classes


def _as_bool(value: Any) -> bool:
    """Coerce a config value to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)
