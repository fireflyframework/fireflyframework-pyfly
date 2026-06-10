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
"""End-to-end test: ConfigClient → Starlette (ASGITransport) → ConfigServer.

The test uses ``httpx.ASGITransport`` so there is no network socket involved,
but the full request/response path — HTTP routing, JSON serialisation, and
``ConfigClient.fetch()``'s reverse-merge of ``propertySources`` — is exercised
for real.  Nothing is mocked.
"""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")
starlette = pytest.importorskip("starlette")

from starlette.applications import Starlette  # noqa: E402

from pyfly.config_server.adapters.starlette import make_starlette_config_server_routes  # noqa: E402
from pyfly.config_server.backend import ConfigSource, InMemoryConfigBackend  # noqa: E402
from pyfly.config_server.client import ConfigClient  # noqa: E402
from pyfly.config_server.server import ConfigServer  # noqa: E402


@pytest.mark.asyncio
async def test_e2e_reverse_merge_precedence() -> None:
    """ConfigClient merges propertySources in reverse so app+profile wins.

    Seed layout
    -----------
    ``orders / prod``  → ``{"host": "prod.db", "port": 5432, "timeout": 30}``
    ``application / default`` → ``{"host": "default.db", "port": 5432, "shared_key": "common"}``

    Expected merged result (app+profile overrides application+default):
    - ``host`` == ``"prod.db"``          (app+profile wins over application+default)
    - ``port`` == 5432                    (same in both — no conflict)
    - ``timeout`` == 30                   (only in app+profile)
    - ``shared_key`` == ``"common"``      (only in application+default — survives)
    """
    # 1. Build the in-process config server.
    backend = InMemoryConfigBackend()
    await backend.save(
        ConfigSource(
            application="orders",
            profile="prod",
            label="main",
            properties={"host": "prod.db", "port": 5432, "timeout": 30},
        )
    )
    await backend.save(
        ConfigSource(
            application="application",
            profile="default",
            label="main",
            properties={"host": "default.db", "port": 5432, "shared_key": "common"},
        )
    )
    server = ConfigServer(backend=backend)
    app = Starlette(routes=make_starlette_config_server_routes(server))

    # 2. Create an httpx client wired directly to the ASGI app — no socket.
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://config") as http_client:
        client = ConfigClient(
            url="http://config",
            application="orders",
            profile="prod",
            label="main",
            http_client=http_client,
        )
        result = await client.fetch()

    # 3. Assert precedence is respected.
    assert result["host"] == "prod.db", "app+profile must override application+default"
    assert result["port"] == 5432
    assert result["timeout"] == 30, "key only in app+profile must survive"
    assert result["shared_key"] == "common", "key only in application+default must be inherited"


@pytest.mark.asyncio
async def test_e2e_missing_config_returns_empty() -> None:
    """A 404 from the server should yield an empty dict, not raise."""
    backend = InMemoryConfigBackend()
    server = ConfigServer(backend=backend)
    app = Starlette(routes=make_starlette_config_server_routes(server))

    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://config") as http_client:
        client = ConfigClient(
            url="http://config",
            application="missing",
            profile="ghost",
            label="main",
            http_client=http_client,
        )
        result = await client.fetch()

    assert result == {}


@pytest.mark.asyncio
async def test_e2e_injected_client_not_closed() -> None:
    """The injected http_client must not be closed after fetch()."""
    backend = InMemoryConfigBackend()
    await backend.save(
        ConfigSource(
            application="svc",
            profile="default",
            label="main",
            properties={"k": "v"},
        )
    )
    server = ConfigServer(backend=backend)
    app = Starlette(routes=make_starlette_config_server_routes(server))

    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    http_client = httpx.AsyncClient(transport=transport, base_url="http://config")
    try:
        client = ConfigClient(
            url="http://config",
            application="svc",
            profile="default",
            label="main",
            http_client=http_client,
        )
        result = await client.fetch()
        assert result == {"k": "v"}
        # Client still open — a second fetch must succeed.
        result2 = await client.fetch()
        assert result2 == {"k": "v"}
    finally:
        await http_client.aclose()
