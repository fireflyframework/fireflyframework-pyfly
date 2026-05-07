# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""ExtensionRegistry — runtime discovery of registered extensions."""

from __future__ import annotations

import asyncio
from typing import Any


class ExtensionRegistry:
    def __init__(self) -> None:
        self._extensions: dict[str, list[tuple[int, Any]]] = {}
        self._lock = asyncio.Lock()

    async def register(self, point_id: str, instance: Any, *, priority: int = 0) -> None:
        async with self._lock:
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
