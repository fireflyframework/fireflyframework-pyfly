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
"""Integration tests proving hexagonal architecture: vendor imports isolated to adapters."""

import subprocess
import sys
from typing import Any
from uuid import UUID

import pytest

from pyfly.client.ports.outbound import HttpClientPort
from pyfly.data.ports.outbound import RepositoryPort, SessionPort
from pyfly.web.ports.outbound import WebServerPort


class TestVendorIsolation:
    """Verify that vendor imports are confined to adapter directories."""

    def test_starlette_only_in_web_adapters_and_actuator_adapters(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "r = subprocess.run("
                "['grep', '-r', 'from starlette', 'src/pyfly/'], "
                "capture_output=True, text=True); "
                "lines = [l for l in r.stdout.strip().split('\\n') if l]; "
                "bad = [l for l in lines "
                "if 'adapters/starlette' not in l and 'adapters/fastapi' not in l "
                "and 'actuator/adapters' not in l "
                "and '/security/' not in l and '/testing/' not in l "
                "and '/admin/' not in l and '/cli/templates/' not in l]; "
                "print('\\n'.join(bad) if bad else 'CLEAN'); "
                "sys.exit(len(bad))",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Starlette leaks found:\n{result.stdout}"

    def test_sqlalchemy_only_in_data_and_cli(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "r = subprocess.run("
                "['grep', '-r', '--include=*.py', 'from sqlalchemy', 'src/pyfly/'], "
                "capture_output=True, text=True); "
                "lines = [l for l in r.stdout.strip().split('\\n') if l]; "
                "bad = [l for l in lines "
                "if '/data/' not in l and '/cli/' not in l "
                "and '/transactional/persistence/' not in l "
                "and '/eventsourcing/' not in l "
                "and '/scheduling/adapters/' not in l "
                "and 'scheduling/auto_configuration' not in l "
                "and '/session/adapters/' not in l "
                "and 'session/auto_configuration' not in l "
                "and '/security/adapters/' not in l "
                "and 'security/auto_configuration' not in l]; "
                "print('\\n'.join(bad) if bad else 'CLEAN'); "
                "sys.exit(len(bad))",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"SQLAlchemy leaks found:\n{result.stdout}"

    def test_httpx_only_in_client_adapters(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "r = subprocess.run("
                "['grep', '-r', 'import httpx', 'src/pyfly/'], "
                "capture_output=True, text=True); "
                "lines = [l for l in r.stdout.strip().split('\\n') if l]; "
                "bad = [l for l in lines "
                "if 'adapters/' not in l and 'admin/server/' not in l "
                "and '/security/' not in l "
                "and '/notifications/providers/' not in l "
                "and '/client/protocols/' not in l "
                "and '/config_server/' not in l "
                "and '/idp/adapters/' not in l "
                "and '/ecm/adapters/' not in l]; "
                "print('\\n'.join(bad) if bad else 'CLEAN'); "
                "sys.exit(len(bad))",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"httpx leaks found:\n{result.stdout}"


class TestPortSwappability:
    """Verify that mock implementations can be used in place of vendor adapters."""

    def test_mock_web_server_port(self):
        class MockWebServer:
            def create_app(self, **kwargs: Any) -> Any:
                return {"type": "mock", **kwargs}

        server = MockWebServer()
        assert isinstance(server, WebServerPort)
        app = server.create_app(port=8080)
        assert app["type"] == "mock"

    @pytest.mark.asyncio
    async def test_mock_http_client_port(self):
        class MockHttpClient:
            async def request(self, method: str, url: str, **kwargs: Any) -> Any:
                return {"method": method, "url": url, "status": 200}

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

        client = MockHttpClient()
        assert isinstance(client, HttpClientPort)
        resp = await client.request("GET", "/test")
        assert resp["status"] == 200

    @pytest.mark.asyncio
    async def test_mock_repository_port(self):
        class MockRepo:
            def __init__(self) -> None:
                self._store: dict[UUID, Any] = {}

            async def save(self, entity: Any) -> Any:
                self._store[entity.id] = entity
                return entity

            async def find_by_id(self, id: UUID) -> Any | None:
                return self._store.get(id)

            async def find_all(self, **filters: Any) -> list[Any]:
                return list(self._store.values())

            async def delete(self, id: UUID) -> None:
                self._store.pop(id, None)

            async def count(self) -> int:
                return len(self._store)

            async def exists(self, id: UUID) -> bool:
                return id in self._store

            async def save_all(self, entities: list[Any]) -> list[Any]:
                for e in entities:
                    self._store[e.id] = e
                return entities

            async def find_all_by_ids(self, ids: list[UUID]) -> list[Any]:
                return [self._store[i] for i in ids if i in self._store]

            async def delete_all(self, ids: list[UUID]) -> int:
                count = 0
                for i in ids:
                    if self._store.pop(i, None) is not None:
                        count += 1
                return count

            async def delete_all_entities(self, entities: list[Any]) -> int:
                count = 0
                for e in entities:
                    if self._store.pop(e.id, None) is not None:
                        count += 1
                return count

        repo = MockRepo()
        assert isinstance(repo, RepositoryPort)

    @pytest.mark.asyncio
    async def test_mock_session_port(self):
        class MockSession:
            async def begin(self) -> Any:
                return self

            async def commit(self) -> None:
                pass

            async def rollback(self) -> None:
                pass

        session = MockSession()
        assert isinstance(session, SessionPort)
        await session.begin()
        await session.commit()
