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
"""Regressions for the SP-11 review: add() raises PluginLoadError; start_all() does not
re-run hooks for plugins already started via start_plugin()."""

from __future__ import annotations

import pytest

from pyfly.kernel.exceptions import PluginException, PluginLoadError
from pyfly.plugins.decorators import plugin
from pyfly.plugins.manager import PluginManager
from pyfly.plugins.models import PluginState


@pytest.mark.asyncio
async def test_add_non_decorated_class_raises_plugin_load_error() -> None:
    class NotAPlugin:
        pass

    manager = PluginManager()
    with pytest.raises(PluginLoadError):
        await manager.add(NotAPlugin)
    assert issubclass(PluginLoadError, PluginException)


@pytest.mark.asyncio
async def test_start_all_does_not_double_run_already_started_plugin() -> None:
    calls: list[str] = []

    @plugin(id="p1")
    class P1:
        async def start(self) -> None:
            calls.append("p1")

    @plugin(id="p2", depends_on=("p1",))
    class P2:
        async def start(self) -> None:
            calls.append("p2")

    manager = PluginManager()
    await manager.add(P1)
    await manager.add(P2)

    await manager.start_plugin("p1")  # p1 STARTED individually
    assert calls == ["p1"]

    await manager.start_all()  # must start only p2 (p1 already STARTED — no double-run)
    assert calls == ["p1", "p2"]

    p1_desc = await manager.get_plugin("p1")
    p2_desc = await manager.get_plugin("p2")
    assert p1_desc is not None and p1_desc.state == PluginState.STARTED
    assert p2_desc is not None and p2_desc.state == PluginState.STARTED
