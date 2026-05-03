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
"""Server subsystem auto-configuration."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
)
from pyfly.server.ports.event_loop import EventLoopPort
from pyfly.server.ports.outbound import ApplicationServerPort


@auto_configuration
@conditional_on_missing_bean(ApplicationServerPort)
class ServerAutoConfiguration:
    """Auto-configures the best available ASGI server.

    Priority: Granian (highest) -> Uvicorn -> Hypercorn (lowest).
    """

    @bean
    @conditional_on_class("granian")
    @conditional_on_missing_bean(ApplicationServerPort)
    def granian_server(self) -> ApplicationServerPort:
        from pyfly.server.adapters.granian.adapter import GranianServerAdapter

        return GranianServerAdapter()

    @bean
    @conditional_on_class("uvicorn")
    @conditional_on_missing_bean(ApplicationServerPort)
    def uvicorn_server(self) -> ApplicationServerPort:
        from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

        return UvicornServerAdapter()

    @bean
    @conditional_on_class("hypercorn")
    @conditional_on_missing_bean(ApplicationServerPort)
    def hypercorn_server(self) -> ApplicationServerPort:
        from pyfly.server.adapters.hypercorn.adapter import HypercornServerAdapter

        return HypercornServerAdapter()


@auto_configuration
@conditional_on_missing_bean(EventLoopPort)
class EventLoopAutoConfiguration:
    """Auto-configures the best available event loop.

    Priority: uvloop (Linux/macOS) -> winloop (Windows) -> asyncio (fallback).
    """

    @bean
    @conditional_on_class("uvloop")
    @conditional_on_missing_bean(EventLoopPort)
    def uvloop(self) -> EventLoopPort:
        from pyfly.server.adapters.event_loop.uvloop_adapter import UvloopEventLoopAdapter

        return UvloopEventLoopAdapter()

    @bean
    @conditional_on_class("winloop")
    @conditional_on_missing_bean(EventLoopPort)
    def winloop(self) -> EventLoopPort:
        from pyfly.server.adapters.event_loop.winloop_adapter import WinloopEventLoopAdapter

        return WinloopEventLoopAdapter()

    @bean
    @conditional_on_missing_bean(EventLoopPort)
    def asyncio_loop(self) -> EventLoopPort:
        from pyfly.server.adapters.event_loop.asyncio_adapter import AsyncioEventLoopAdapter

        return AsyncioEventLoopAdapter()
