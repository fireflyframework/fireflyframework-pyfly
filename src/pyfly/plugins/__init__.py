# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly plugin system — pluggable modules with extension points and lifecycle.

Mirrors ``org.fireflyframework.plugins``: ``@plugin`` / ``@extension_point`` /
``@extension`` decorators, dependency resolution, lifecycle (load → init → start
→ stop → unload), an ``ExtensionRegistry`` for runtime discovery.
"""

from __future__ import annotations

from pyfly.plugins.decorators import (
    Extension,
    ExtensionPoint,
    Plugin,
    extension,
    extension_point,
    plugin,
)
from pyfly.plugins.manager import PluginManager
from pyfly.plugins.models import PluginDescriptor, PluginState
from pyfly.plugins.registry import ExtensionRegistry
from pyfly.plugins.resolver import PluginDependencyResolver, PluginResolutionError

__all__ = [
    "Extension",
    "ExtensionPoint",
    "ExtensionRegistry",
    "Plugin",
    "PluginDependencyResolver",
    "PluginDescriptor",
    "PluginManager",
    "PluginResolutionError",
    "PluginState",
    "extension",
    "extension_point",
    "plugin",
]
