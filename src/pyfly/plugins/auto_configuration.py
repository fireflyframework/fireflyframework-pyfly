# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the plugins module."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.plugins.manager import PluginManager
from pyfly.plugins.registry import ExtensionRegistry


@auto_configuration
@conditional_on_property("pyfly.plugins.enabled", having_value="true")
class PluginsAutoConfiguration:
    @bean
    def extension_registry(self) -> ExtensionRegistry:
        return ExtensionRegistry()

    @bean
    def plugin_manager(self, registry: ExtensionRegistry) -> PluginManager:
        return PluginManager(registry=registry)
