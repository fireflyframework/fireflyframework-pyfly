# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PluginManager — load, init, start, stop, unload plugins in dependency order."""

from __future__ import annotations

import asyncio
import datetime
import inspect
from typing import Any

from pyfly.kernel.exceptions import PluginLoadError, PluginStartError, PluginStateError, PluginStopError
from pyfly.plugins.decorators import Plugin
from pyfly.plugins.models import PluginDescriptor, PluginState
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
        self._descriptors: dict[str, PluginDescriptor] = {}
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

    async def add(self, plugin_class: type) -> None:
        meta = getattr(plugin_class, "__pyfly_plugin__", None)
        if meta is None:
            msg = f"{plugin_class.__qualname__} is not @plugin-decorated"
            raise PluginLoadError(msg)
        instance = plugin_class()
        now = datetime.datetime.now(tz=datetime.UTC)
        descriptor = PluginDescriptor(
            id=meta.id,
            plugin=meta,
            state=PluginState.LOADED,
            loaded_at=now,
            last_state_change=now,
        )
        async with self._lock:
            self._plugins[meta.id] = meta
            self._instances[meta.id] = instance
            self._descriptors[meta.id] = descriptor

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _transitive_deps(self, plugin_id: str) -> set[str]:
        """Return the set of all transitive dependency ids for *plugin_id*."""
        visited: set[str] = set()
        queue = list(self._plugins[plugin_id].depends_on)
        while queue:
            dep = queue.pop()
            if dep in visited:
                continue
            visited.add(dep)
            if dep in self._plugins:
                queue.extend(self._plugins[dep].depends_on)
        return visited

    def _transitive_dependents(self, plugin_id: str) -> set[str]:
        """Return the set of all plugins that (transitively) depend on *plugin_id*."""
        # Start with direct dependents then fan out.
        dependents: set[str] = set()
        changed = True
        while changed:
            changed = False
            for pid, plugin in self._plugins.items():
                if pid in dependents:
                    continue
                deps = set(plugin.depends_on)
                if plugin_id in deps or deps & dependents:
                    dependents.add(pid)
                    changed = True
        return dependents

    async def _run_hooks(self, instance: Any, *hooks: str) -> None:
        for hook in hooks:
            fn = getattr(instance, hook, None)
            if fn is None:
                continue
            result = fn()
            if inspect.isawaitable(result):
                await result

    def _transition(self, plugin_id: str, state: PluginState, reason: str | None = None) -> None:
        desc = self._descriptors[plugin_id]
        desc.state = state
        desc.last_state_change = datetime.datetime.now(tz=datetime.UTC)
        if reason is not None:
            desc.failed_reason = reason

    # ------------------------------------------------------------------
    # Per-plugin lifecycle
    # ------------------------------------------------------------------

    async def start_plugin(self, plugin_id: str) -> None:
        """Start *plugin_id* and all its transitive dependencies (in dep order).

        Skips plugins already in STARTED state. On hook failure, marks the
        plugin FAILED and raises PluginStartError.
        """
        async with self._lock:
            if plugin_id not in self._plugins:
                msg = f"Unknown plugin id: {plugin_id!r}"
                raise PluginStateError(msg)
            full_order = PluginDependencyResolver.order(self._plugins)
            subset = {plugin_id} | self._transitive_deps(plugin_id)
            to_start = [pid for pid in full_order if pid in subset]

        for pid in to_start:
            async with self._lock:
                desc = self._descriptors[pid]
                if desc.state == PluginState.STARTED:
                    continue
            instance = self._instances[pid]
            try:
                await self._run_hooks(instance, "init", "start")
            except Exception as exc:
                async with self._lock:
                    self._transition(pid, PluginState.FAILED, str(exc))
                msg = f"Plugin {pid!r} failed to start: {exc}"
                raise PluginStartError(msg) from exc
            async with self._lock:
                self._transition(pid, PluginState.STARTED)

    async def stop_plugin(self, plugin_id: str) -> None:
        """Stop *plugin_id* and all plugins that (transitively) depend on it.

        Processes dependents first (reverse cascade), then this plugin.
        Skips plugins already STOPPED or LOADED. On hook failure, marks FAILED
        and raises PluginStopError.
        """
        async with self._lock:
            if plugin_id not in self._plugins:
                msg = f"Unknown plugin id: {plugin_id!r}"
                raise PluginStateError(msg)
            full_order = PluginDependencyResolver.order(self._plugins)
            subset = {plugin_id} | self._transitive_dependents(plugin_id)
            # Reverse so dependents are stopped before their dependencies.
            to_stop = [pid for pid in reversed(full_order) if pid in subset]

        for pid in to_stop:
            async with self._lock:
                desc = self._descriptors[pid]
                if desc.state in (PluginState.STOPPED, PluginState.LOADED):
                    continue
            instance = self._instances[pid]
            try:
                await self._run_hooks(instance, "stop", "unload")
            except Exception as exc:
                async with self._lock:
                    self._transition(pid, PluginState.FAILED, str(exc))
                msg = f"Plugin {pid!r} failed to stop: {exc}"
                raise PluginStopError(msg) from exc
            async with self._lock:
                self._transition(pid, PluginState.STOPPED)

    async def get_plugin(self, plugin_id: str) -> PluginDescriptor | None:
        """Return the PluginDescriptor for *plugin_id*, or None if not found."""
        async with self._lock:
            return self._descriptors.get(plugin_id)

    # ------------------------------------------------------------------
    # Bulk lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        async with self._lock:
            if self._started:
                return
            order = PluginDependencyResolver.order(self._plugins)
        for pid in order:
            # Skip plugins already started via an earlier start_plugin() call so
            # their init/start hooks don't run twice on a mixed start path.
            if self._descriptors[pid].state == PluginState.STARTED:
                continue
            instance = self._instances[pid]
            for hook in ("init", "start"):
                fn = getattr(instance, hook, None)
                if fn is None:
                    continue
                result = fn()
                if inspect.isawaitable(result):
                    await result
            async with self._lock:
                self._transition(pid, PluginState.STARTED)
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
                self._transition(pid, PluginState.STOPPED)
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
            self._descriptors.pop(plugin_id, None)
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
