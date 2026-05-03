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
"""Tests for ApplicationServerPort and EventLoopPort protocols."""

from __future__ import annotations

import pytest

from pyfly.server.ports.event_loop import EventLoopPort
from pyfly.server.ports.outbound import ApplicationServerPort
from pyfly.server.types import ServerInfo


class _FakeServer:
    """Duck-typed ApplicationServerPort."""

    def serve(self, app, config):
        pass

    async def serve_async(self, app, config):
        pass

    def shutdown(self):
        pass

    @property
    def server_info(self) -> ServerInfo:
        return ServerInfo(
            name="fake",
            version="1.0.0",
            workers=1,
            event_loop="asyncio",
            http_protocol="h1",
            host="0.0.0.0",
            port=8000,
        )


class _FakeEventLoop:
    """Duck-typed EventLoopPort."""

    def install(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "fake"


class _NotAServer:
    pass


class TestApplicationServerPort:
    def test_duck_typed_class_is_server_port(self):
        assert isinstance(_FakeServer(), ApplicationServerPort)

    def test_non_server_is_not_server_port(self):
        assert not isinstance(_NotAServer(), ApplicationServerPort)


class TestEventLoopPort:
    def test_duck_typed_class_is_event_loop_port(self):
        assert isinstance(_FakeEventLoop(), EventLoopPort)

    def test_non_loop_is_not_event_loop_port(self):
        assert not isinstance(_NotAServer(), EventLoopPort)


class TestServerInfo:
    def test_server_info_is_frozen(self):
        info = ServerInfo(
            name="granian",
            version="2.7.1",
            workers=4,
            event_loop="uvloop",
            http_protocol="auto",
            host="0.0.0.0",
            port=8000,
        )
        assert info.name == "granian"
        with pytest.raises(AttributeError):
            info.name = "other"  # type: ignore[misc]
