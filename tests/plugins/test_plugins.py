# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the plugin system."""

from __future__ import annotations

import pytest

from pyfly.plugins.decorators import extension, extension_point, plugin
from pyfly.plugins.manager import PluginManager
from pyfly.plugins.resolver import PluginDependencyResolver, PluginResolutionError


@pytest.mark.asyncio
async def test_lifecycle_in_dependency_order() -> None:
    sequence: list[str] = []

    @plugin(id="a")
    class A:
        async def start(self) -> None:
            sequence.append("a-start")

        async def stop(self) -> None:
            sequence.append("a-stop")

    @plugin(id="b", depends_on=("a",))
    class B:
        async def start(self) -> None:
            sequence.append("b-start")

        async def stop(self) -> None:
            sequence.append("b-stop")

    manager = PluginManager()
    await manager.add(A)
    await manager.add(B)
    await manager.start_all()
    await manager.stop_all()
    assert sequence == ["a-start", "b-start", "b-stop", "a-stop"]


@pytest.mark.asyncio
async def test_extensions_register_to_extension_points() -> None:
    @extension_point(id="formatters")
    class _FormatterPoint: ...

    @plugin(id="ext-host")
    class Host:
        @extension(point="formatters", priority=10)
        class JsonFormatter:
            name = "json"

        @extension(point="formatters", priority=5)
        class XmlFormatter:
            name = "xml"

    manager = PluginManager()
    await manager.add(Host)
    instances = await manager.registry.get("formatters")
    names = [i.name for i in instances]
    # higher priority first
    assert names == ["json", "xml"]


def test_dependency_cycle_rejected() -> None:
    @plugin(id="a", depends_on=("b",))
    class A: ...

    @plugin(id="b", depends_on=("a",))
    class B: ...

    plugins = {"a": A.__pyfly_plugin__, "b": B.__pyfly_plugin__}  # type: ignore[attr-defined]
    with pytest.raises(PluginResolutionError):
        PluginDependencyResolver.order(plugins)
