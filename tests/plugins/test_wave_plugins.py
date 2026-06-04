# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Regression tests for plugin fixes.

#218 — extension-point registration + extension type validation.
#219 — unloading a plugin unregisters its extensions.
"""

from __future__ import annotations

import pytest

from pyfly.plugins.decorators import extension, extension_point, plugin
from pyfly.plugins.manager import PluginManager
from pyfly.plugins.registry import ExtensionRegistry

# ---------------------------------------------------------------------------
# #218 — extension point registration + validation
# ---------------------------------------------------------------------------


class TestExtensionPointValidation:
    @pytest.mark.asyncio
    async def test_register_point_then_validate(self):
        @extension_point(id="greeters")
        class GreeterPoint: ...

        class EnglishGreeter(GreeterPoint): ...

        class NotAGreeter: ...

        reg = ExtensionRegistry()
        await reg.register_extension_point("greeters", GreeterPoint)
        assert await reg.has_extension_point("greeters") is True
        assert "greeters" in await reg.extension_point_ids()

        await reg.register("greeters", EnglishGreeter())  # conforms — ok
        assert len(await reg.get("greeters")) == 1

        with pytest.raises(ValueError, match="does not implement extension point"):
            await reg.register("greeters", NotAGreeter())

    @pytest.mark.asyncio
    async def test_unknown_point_is_lenient(self):
        # Backward-compatible: registering against an id with no declared point
        # type is still accepted (no validation).
        reg = ExtensionRegistry()
        await reg.register("freeform", object())
        assert len(await reg.get("freeform")) == 1
        assert await reg.has_extension_point("freeform") is False

    @pytest.mark.asyncio
    async def test_manager_registers_inner_extension_point(self):
        @plugin(id="host-ep")
        class Host:
            @extension_point(id="formatters-x")
            class FormatterPoint: ...

        mgr = PluginManager()
        await mgr.add(Host)
        assert await mgr.registry.has_extension_point("formatters-x") is True
        assert "formatters-x" in await mgr.registry.extension_point_ids()

    @pytest.mark.asyncio
    async def test_manager_rejects_nonconforming_extension(self):
        @plugin(id="host-bad")
        class Host:
            @extension_point(id="codecs")
            class CodecPoint: ...

            @extension(point="codecs")
            class BadCodec:  # does NOT implement CodecPoint
                ...

        mgr = PluginManager()
        with pytest.raises(ValueError, match="does not implement extension point"):
            await mgr.add(Host)


# ---------------------------------------------------------------------------
# #219 — unload unregisters extensions
# ---------------------------------------------------------------------------


class TestPluginUnload:
    @pytest.mark.asyncio
    async def test_remove_unregisters_extensions(self):
        @plugin(id="ext-host")
        class ExtHost:
            @extension(point="things", priority=1)
            class ThingA:
                name = "a"

        mgr = PluginManager()
        await mgr.add(ExtHost)
        assert len(await mgr.registry.get("things")) == 1

        assert await mgr.remove("ext-host") is True
        assert await mgr.registry.get("things") == []
        assert "ext-host" not in [p.id for p in mgr.list_plugins()]

    @pytest.mark.asyncio
    async def test_remove_unknown_returns_false(self):
        mgr = PluginManager()
        assert await mgr.remove("nope") is False

    @pytest.mark.asyncio
    async def test_remove_runs_unload_hook(self):
        calls: list[str] = []

        @plugin(id="uh")
        class UH:
            async def unload(self) -> None:
                calls.append("unloaded")

        mgr = PluginManager()
        await mgr.add(UH)
        await mgr.remove("uh")
        assert calls == ["unloaded"]

    @pytest.mark.asyncio
    async def test_unload_all_clears_registry(self):
        @plugin(id="p1")
        class P1:
            @extension(point="x")
            class E1: ...

        @plugin(id="p2", depends_on=("p1",))
        class P2:
            @extension(point="x")
            class E2: ...

        mgr = PluginManager()
        await mgr.add(P1)
        await mgr.add(P2)
        assert len(await mgr.registry.get("x")) == 2

        await mgr.unload_all()
        assert await mgr.registry.get("x") == []
        assert mgr.list_plugins() == []
