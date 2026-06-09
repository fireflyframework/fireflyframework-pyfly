# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PluginDependencyResolver — order plugins by their declared dependencies."""

from __future__ import annotations

from collections import deque

from pyfly.kernel.exceptions import PluginException
from pyfly.plugins.decorators import Plugin


class PluginResolutionError(PluginException):
    pass


class PluginDependencyResolver:
    @staticmethod
    def order(plugins: dict[str, Plugin]) -> list[str]:
        in_degree: dict[str, int] = {pid: 0 for pid in plugins}
        for plugin in plugins.values():
            for dep in plugin.depends_on:
                if dep not in plugins:
                    msg = f"plugin '{plugin.id}' depends on missing plugin '{dep}'"
                    raise PluginResolutionError(msg)
                in_degree[plugin.id] += 1

        ready: deque[str] = deque(sorted(pid for pid, d in in_degree.items() if d == 0))
        ordered: list[str] = []
        while ready:
            current = ready.popleft()
            ordered.append(current)
            for pid, plugin in plugins.items():
                if current in plugin.depends_on:
                    in_degree[pid] -= 1
                    if in_degree[pid] == 0:
                        ready.append(pid)

        if len(ordered) != len(plugins):
            msg = "plugin dependency cycle detected"
            raise PluginResolutionError(msg)
        return ordered
