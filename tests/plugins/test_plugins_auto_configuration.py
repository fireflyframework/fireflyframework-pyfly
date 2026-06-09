# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for PluginsAutoConfiguration bean wiring."""

from __future__ import annotations

from pyfly.plugins.auto_configuration import PluginsAutoConfiguration
from pyfly.plugins.manager import PluginManager
from pyfly.plugins.registry import ExtensionRegistry


class TestPluginsAutoConfiguration:
    def test_has_auto_configuration_marker(self) -> None:
        assert getattr(PluginsAutoConfiguration, "__pyfly_auto_configuration__", False) is True

    def test_has_configuration_stereotype(self) -> None:
        assert getattr(PluginsAutoConfiguration, "__pyfly_stereotype__", None) == "configuration"

    def test_has_on_property_condition(self) -> None:
        conditions = getattr(PluginsAutoConfiguration, "__pyfly_conditions__", [])
        types = [c["type"] for c in conditions]
        assert "on_property" in types

    def test_on_property_key_is_pyfly_plugins_enabled(self) -> None:
        conditions = getattr(PluginsAutoConfiguration, "__pyfly_conditions__", [])
        prop_cond = next(c for c in conditions if c["type"] == "on_property")
        assert prop_cond["key"] == "pyfly.plugins.enabled"
        assert prop_cond["having_value"] == "true"

    def test_extension_registry_bean_produces_registry(self) -> None:
        cfg = PluginsAutoConfiguration()
        reg = cfg.extension_registry()
        assert isinstance(reg, ExtensionRegistry)

    def test_plugin_manager_bean_produces_manager(self) -> None:
        cfg = PluginsAutoConfiguration()
        reg = cfg.extension_registry()
        mgr = cfg.plugin_manager(reg)
        assert isinstance(mgr, PluginManager)

    def test_plugin_manager_uses_provided_registry(self) -> None:
        cfg = PluginsAutoConfiguration()
        reg = cfg.extension_registry()
        mgr = cfg.plugin_manager(reg)
        assert mgr.registry is reg
