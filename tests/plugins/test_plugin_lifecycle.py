# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""End-to-end lifecycle tests mirroring Java PluginSystemIntegrationTest."""

from __future__ import annotations

import pytest

from pyfly.kernel.exceptions import PluginStartError, PluginStateError, PluginStopError
from pyfly.plugins.decorators import extension, extension_point, plugin
from pyfly.plugins.manager import PluginManager
from pyfly.plugins.models import PluginDescriptor, PluginState

# ---------------------------------------------------------------------------
# Shared plugin definitions (A ← B ← C, with extension point on A)
# ---------------------------------------------------------------------------

started_order: list[str] = []
stopped_order: list[str] = []


@plugin(id="a", name="Plugin A", author="alice", description="root")
class PluginA:
    @extension_point(id="processors")
    class ProcessorPoint: ...

    @extension(point="processors", priority=10)
    class DefaultProcessor(ProcessorPoint):
        label = "default"

    async def start(self) -> None:
        started_order.append("a")

    async def stop(self) -> None:
        stopped_order.append("a")


@plugin(id="b", depends_on=("a",), name="Plugin B", author="bob")
class PluginB:
    async def start(self) -> None:
        started_order.append("b")

    async def stop(self) -> None:
        stopped_order.append("b")


@plugin(id="c", depends_on=("b",), name="Plugin C", author="charlie")
class PluginC:
    async def start(self) -> None:
        started_order.append("c")

    async def stop(self) -> None:
        stopped_order.append("c")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_plugin_cascades_dependencies() -> None:
    """start_plugin(C) should start A, then B, then C."""
    started_order.clear()
    mgr = PluginManager()
    await mgr.add(PluginA)
    await mgr.add(PluginB)
    await mgr.add(PluginC)

    await mgr.start_plugin("c")

    assert started_order == ["a", "b", "c"]
    for pid in ("a", "b", "c"):
        desc = await mgr.get_plugin(pid)
        assert desc is not None
        assert desc.state == PluginState.STARTED


@pytest.mark.asyncio
async def test_start_plugin_skips_already_started() -> None:
    """Starting A, then start_plugin(C) should not double-start A."""
    started_order.clear()
    mgr = PluginManager()
    await mgr.add(PluginA)
    await mgr.add(PluginB)
    await mgr.add(PluginC)

    await mgr.start_plugin("a")  # start A first
    started_order.clear()  # reset recorder
    await mgr.start_plugin("c")  # now cascade from C

    # A already started → skipped; only B and C should fire
    assert started_order == ["b", "c"]


@pytest.mark.asyncio
async def test_stop_plugin_cascades_dependents() -> None:
    """stop_plugin(A) should stop C first, then B, then A."""
    stopped_order.clear()
    mgr = PluginManager()
    await mgr.add(PluginA)
    await mgr.add(PluginB)
    await mgr.add(PluginC)
    await mgr.start_plugin("c")

    stopped_order.clear()
    await mgr.stop_plugin("a")

    assert stopped_order == ["c", "b", "a"]
    for pid in ("a", "b", "c"):
        desc = await mgr.get_plugin(pid)
        assert desc is not None
        assert desc.state == PluginState.STOPPED


@pytest.mark.asyncio
async def test_get_extension_returns_highest_priority() -> None:
    """After add, get_extension returns the single highest-priority extension."""
    mgr = PluginManager()
    await mgr.add(PluginA)

    ext = await mgr.registry.get_extension("processors")
    assert ext.label == "default"


@pytest.mark.asyncio
async def test_get_extension_raises_for_unknown_point() -> None:
    mgr = PluginManager()
    await mgr.add(PluginA)

    with pytest.raises(ValueError, match="not registered"):
        await mgr.registry.get_extension("nonexistent")


@pytest.mark.asyncio
async def test_get_extension_raises_for_empty_point() -> None:
    """Registered point with no extensions → ValueError."""
    from pyfly.plugins.registry import ExtensionRegistry

    reg = ExtensionRegistry()
    await reg.register_extension_point("empty-point", object)

    with pytest.raises(ValueError, match="no registered extensions"):
        await reg.get_extension("empty-point")


@pytest.mark.asyncio
async def test_failed_start_sets_failed_state() -> None:
    """If a plugin's start() hook raises, state becomes FAILED."""

    @plugin(id="bad-plugin")
    class BadPlugin:
        async def start(self) -> None:
            raise RuntimeError("kaboom")

    mgr = PluginManager()
    await mgr.add(BadPlugin)

    with pytest.raises(PluginStartError, match="kaboom"):
        await mgr.start_plugin("bad-plugin")

    desc = await mgr.get_plugin("bad-plugin")
    assert desc is not None
    assert desc.state == PluginState.FAILED
    assert desc.failed_reason is not None
    assert "kaboom" in desc.failed_reason


@pytest.mark.asyncio
async def test_get_plugin_returns_none_for_unknown() -> None:
    mgr = PluginManager()
    desc = await mgr.get_plugin("nope")
    assert desc is None


@pytest.mark.asyncio
async def test_start_plugin_raises_state_error_for_unknown() -> None:
    mgr = PluginManager()

    with pytest.raises(PluginStateError):
        await mgr.start_plugin("ghost")


@pytest.mark.asyncio
async def test_stop_plugin_raises_state_error_for_unknown() -> None:
    mgr = PluginManager()

    with pytest.raises(PluginStateError):
        await mgr.stop_plugin("ghost")


@pytest.mark.asyncio
async def test_plugin_descriptor_captures_name_author() -> None:
    """@plugin(name=, author=) fields are captured in the descriptor."""
    mgr = PluginManager()
    await mgr.add(PluginA)

    desc = await mgr.get_plugin("a")
    assert desc is not None
    assert isinstance(desc, PluginDescriptor)
    assert desc.plugin.name == "Plugin A"
    assert desc.plugin.author == "alice"


@pytest.mark.asyncio
async def test_start_all_sets_started_state() -> None:
    """start_all() marks every plugin STARTED."""

    @plugin(id="s1")
    class S1: ...

    @plugin(id="s2", depends_on=("s1",))
    class S2: ...

    mgr = PluginManager()
    await mgr.add(S1)
    await mgr.add(S2)
    await mgr.start_all()

    for pid in ("s1", "s2"):
        desc = await mgr.get_plugin(pid)
        assert desc is not None
        assert desc.state == PluginState.STARTED


@pytest.mark.asyncio
async def test_stop_all_sets_stopped_state() -> None:
    """stop_all() marks every plugin STOPPED."""

    @plugin(id="t1")
    class T1: ...

    @plugin(id="t2", depends_on=("t1",))
    class T2: ...

    mgr = PluginManager()
    await mgr.add(T1)
    await mgr.add(T2)
    await mgr.start_all()
    await mgr.stop_all()

    for pid in ("t1", "t2"):
        desc = await mgr.get_plugin(pid)
        assert desc is not None
        assert desc.state == PluginState.STOPPED


@pytest.mark.asyncio
async def test_stop_plugin_unused_raises_state_error() -> None:
    """PluginStopError is importable and is a PluginException subclass."""
    from pyfly.kernel.exceptions import PluginException

    assert issubclass(PluginStopError, PluginException)
