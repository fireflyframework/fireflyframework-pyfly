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
"""Tests for event loop adapters."""

from __future__ import annotations

import pytest

from pyfly.server.adapters.event_loop.asyncio_adapter import AsyncioEventLoopAdapter
from pyfly.server.ports.event_loop import EventLoopPort


class TestAsyncioEventLoopAdapter:
    def test_is_event_loop_port(self):
        adapter = AsyncioEventLoopAdapter()
        assert isinstance(adapter, EventLoopPort)

    def test_name(self):
        adapter = AsyncioEventLoopAdapter()
        assert adapter.name == "asyncio"

    def test_install_is_noop(self):
        adapter = AsyncioEventLoopAdapter()
        adapter.install()


class TestUvloopEventLoopAdapter:
    def test_is_event_loop_port(self):
        try:
            from pyfly.server.adapters.event_loop.uvloop_adapter import UvloopEventLoopAdapter
        except ImportError:
            pytest.skip("uvloop not installed")
        adapter = UvloopEventLoopAdapter()
        assert isinstance(adapter, EventLoopPort)

    def test_name(self):
        try:
            from pyfly.server.adapters.event_loop.uvloop_adapter import UvloopEventLoopAdapter
        except ImportError:
            pytest.skip("uvloop not installed")
        adapter = UvloopEventLoopAdapter()
        assert adapter.name == "uvloop"


class TestWinloopEventLoopAdapter:
    def test_is_event_loop_port(self):
        try:
            from pyfly.server.adapters.event_loop.winloop_adapter import WinloopEventLoopAdapter
        except ImportError:
            pytest.skip("winloop not installed")
        adapter = WinloopEventLoopAdapter()
        assert isinstance(adapter, EventLoopPort)

    def test_name(self):
        try:
            from pyfly.server.adapters.event_loop.winloop_adapter import WinloopEventLoopAdapter
        except ImportError:
            pytest.skip("winloop not installed")
        adapter = WinloopEventLoopAdapter()
        assert adapter.name == "winloop"
