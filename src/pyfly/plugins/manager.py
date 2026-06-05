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
        # (point_id, instance) pairs each plugin registered, so unload can
        # unregister exactly what it added (audit #219).
        self._registered: dict[str, list[tuple[str, Any]]] = {}
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

        # Register @extension_point declarations first (the decorated class is the
        # point's interface type) so extension type-validation can apply (#218).
        for attr_name in dir(plugin_class):
            attr = getattr(plugin_class, attr_name, None)
            ep_meta = getattr(attr, "__pyfly_extension_point__", None)
            if ep_meta is not None and inspect.isclass(attr):
                await self._registry.register_extension_point(ep_meta.id, attr)

        # Discover @extension on inner classes / methods, tracking what we add.
        registered: list[tuple[str, Any]] = []
        for attr_name in dir(plugin_class):
            attr = getattr(plugin_class, attr_name, None)
            ext_meta = getattr(attr, "__pyfly_extension__", None)
            if ext_meta is not None and inspect.isclass(attr):
                ext_instance = attr()
                await self._registry.register(ext_meta.point, ext_instance, priority=ext_meta.priority)
                registered.append((ext_meta.point, ext_instance))
        async with self._lock:
            self._registered[meta.id] = registered

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

    async def remove(self, plugin_id: str) -> bool:
        """Unload a single plugin: run its ``unload`` hook, unregister its
        extensions, and forget it (audit #219).

        Mirrors Java's ``unloadPlugin`` — extensions a plugin contributed no
        longer leak in the registry after it is unloaded. Returns ``False`` when
        the plugin is unknown.
        """
        async with self._lock:
            instance = self._instances.get(plugin_id)
            if instance is None:
                return False
            registered = self._registered.pop(plugin_id, [])

        fn = getattr(instance, "unload", None)
        if fn is not None:
            result = fn()
            if inspect.isawaitable(result):
                await result

        for point_id, ext_instance in registered:
            await self._registry.unregister(point_id, ext_instance)

        async with self._lock:
            self._plugins.pop(plugin_id, None)
            self._instances.pop(plugin_id, None)
        return True

    async def unload_all(self) -> None:
        """Unload every plugin (reverse dependency order), clearing the registry."""
        async with self._lock:
            order = PluginDependencyResolver.order(self._plugins)
        for pid in reversed(order):
            await self.remove(pid)
        async with self._lock:
            self._started = False

    def list_plugins(self) -> list[Plugin]:
        return list(self._plugins.values())
