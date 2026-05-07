# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PluginManager — load, init, start, stop, unload plugins in dependency order."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from pyfly.plugins.decorators import Plugin
from pyfly.plugins.registry import ExtensionRegistry
from pyfly.plugins.resolver import PluginDependencyResolver


class PluginManager:
    def __init__(self, registry: ExtensionRegistry | None = None) -> None:
        self._registry = registry or ExtensionRegistry()
        self._plugins: dict[str, Plugin] = {}
        self._instances: dict[str, Any] = {}
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

    async def add(self, plugin_class: type) -> None:
        meta = getattr(plugin_class, "__pyfly_plugin__", None)
        if meta is None:
            msg = f"{plugin_class.__qualname__} is not @plugin-decorated"
            raise ValueError(msg)
        instance = plugin_class()
        async with self._lock:
            self._plugins[meta.id] = meta
            self._instances[meta.id] = instance
        # Discover @extension on inner classes / methods.
        for attr_name in dir(plugin_class):
            attr = getattr(plugin_class, attr_name, None)
            ext_meta = getattr(attr, "__pyfly_extension__", None)
            if ext_meta is not None and inspect.isclass(attr):
                await self._registry.register(ext_meta.point, attr(), priority=ext_meta.priority)

    async def start_all(self) -> None:
        async with self._lock:
            if self._started:
                return
            order = PluginDependencyResolver.order(self._plugins)
        for pid in order:
            instance = self._instances[pid]
            for hook in ("init", "start"):
                fn = getattr(instance, hook, None)
                if fn is None:
                    continue
                result = fn()
                if inspect.isawaitable(result):
                    await result
        async with self._lock:
            self._started = True

    async def stop_all(self) -> None:
        async with self._lock:
            if not self._started:
                return
            order = PluginDependencyResolver.order(self._plugins)
        for pid in reversed(order):
            instance = self._instances[pid]
            for hook in ("stop", "unload"):
                fn = getattr(instance, hook, None)
                if fn is None:
                    continue
                result = fn()
                if inspect.isawaitable(result):
                    await result
        async with self._lock:
            self._started = False

    def list_plugins(self) -> list[Plugin]:
        return list(self._plugins.values())
