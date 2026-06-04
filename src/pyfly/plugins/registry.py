# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""ExtensionRegistry — runtime discovery of registered extensions."""

from __future__ import annotations

import asyncio
from typing import Any


class ExtensionRegistry:
    def __init__(self) -> None:
        self._extensions: dict[str, list[tuple[int, Any]]] = {}
        self._points: dict[str, type] = {}
        self._lock = asyncio.Lock()

    async def register_extension_point(self, point_id: str, point_type: type) -> None:
        """Record an extension point's id and its interface type (audit #218).

        Once a point is registered, ``register`` validates that contributed
        extensions are instances of ``point_type``, mirroring Java's
        DefaultExtensionRegistry. Extensions for ids with no registered point
        type remain accepted (lenient, backward-compatible).
        """
        async with self._lock:
            self._points[point_id] = point_type

    async def has_extension_point(self, point_id: str) -> bool:
        async with self._lock:
            return point_id in self._points

    async def extension_point_ids(self) -> list[str]:
        async with self._lock:
            return list(self._points.keys())

    async def register(self, point_id: str, instance: Any, *, priority: int = 0) -> None:
        async with self._lock:
            point_type = self._points.get(point_id)
            if point_type is not None and not isinstance(instance, point_type):
                msg = (
                    f"Extension {type(instance).__qualname__!r} does not implement extension "
                    f"point {point_id!r} ({point_type.__qualname__})"
                )
                raise ValueError(msg)
            entries = self._extensions.setdefault(point_id, [])
            entries.append((priority, instance))
            entries.sort(key=lambda x: -x[0])

    async def unregister(self, point_id: str, instance: Any) -> bool:
        async with self._lock:
            entries = self._extensions.get(point_id, [])
            for i, (_, inst) in enumerate(entries):
                if inst is instance:
                    entries.pop(i)
                    return True
        return False

    async def get(self, point_id: str) -> list[Any]:
        async with self._lock:
            return [inst for _, inst in self._extensions.get(point_id, [])]

    async def points(self) -> list[str]:
        async with self._lock:
            return list(self._extensions.keys())
