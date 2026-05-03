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
"""Tests for server auto-configuration."""

from __future__ import annotations

from pyfly.server.auto_configuration import EventLoopAutoConfiguration, ServerAutoConfiguration


class TestServerAutoConfiguration:
    def test_has_auto_configuration_marker(self):
        assert getattr(ServerAutoConfiguration, "__pyfly_auto_configuration__", False)

    def test_has_granian_bean(self):
        assert hasattr(ServerAutoConfiguration, "granian_server")
        assert getattr(ServerAutoConfiguration.granian_server, "__pyfly_bean__", False)

    def test_has_uvicorn_bean(self):
        assert hasattr(ServerAutoConfiguration, "uvicorn_server")
        assert getattr(ServerAutoConfiguration.uvicorn_server, "__pyfly_bean__", False)

    def test_has_hypercorn_bean(self):
        assert hasattr(ServerAutoConfiguration, "hypercorn_server")
        assert getattr(ServerAutoConfiguration.hypercorn_server, "__pyfly_bean__", False)


class TestEventLoopAutoConfiguration:
    def test_has_auto_configuration_marker(self):
        assert getattr(EventLoopAutoConfiguration, "__pyfly_auto_configuration__", False)

    def test_has_uvloop_bean(self):
        assert hasattr(EventLoopAutoConfiguration, "uvloop")
        assert getattr(EventLoopAutoConfiguration.uvloop, "__pyfly_bean__", False)

    def test_has_asyncio_bean(self):
        assert hasattr(EventLoopAutoConfiguration, "asyncio_loop")
        assert getattr(EventLoopAutoConfiguration.asyncio_loop, "__pyfly_bean__", False)
