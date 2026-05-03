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
"""Tests for ServerProvider."""

from __future__ import annotations

from pyfly.admin.providers.server_provider import ServerProvider
from pyfly.server.types import ServerInfo


class _FakeServer:
    @property
    def server_info(self) -> ServerInfo:
        return ServerInfo(
            name="granian",
            version="2.7.1",
            workers=4,
            event_loop="uvloop",
            http_protocol="auto",
            host="0.0.0.0",
            port=8000,
        )


class TestServerProvider:
    async def test_get_server_info(self):
        provider = ServerProvider(_FakeServer())
        info = await provider.get_server_info()
        assert info["name"] == "granian"
        assert info["version"] == "2.7.1"
        assert info["workers"] == 4
        assert info["event_loop"] == "uvloop"

    async def test_get_server_info_without_server(self):
        provider = ServerProvider(None)
        info = await provider.get_server_info()
        assert info["name"] == "unknown"
