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
"""Regression tests for config-server fixes.

#85 — ConfigServer.fetch emits the full app/default/application overlay set.
#88 — the filesystem backend uses a configured, persistent root.
"""

from __future__ import annotations

import pathlib

import pytest

from pyfly.config_server.auto_configuration import ConfigServerAutoConfiguration
from pyfly.config_server.backend import ConfigSource, FilesystemConfigBackend, InMemoryConfigBackend
from pyfly.config_server.server import ConfigServer
from pyfly.core.config import Config


async def _save(backend: InMemoryConfigBackend, app: str, profile: str, props: dict) -> None:
    await backend.save(ConfigSource(application=app, profile=profile, properties=props))


class TestFetchOverlays:
    @pytest.mark.asyncio
    async def test_overlays_app_and_application_defaults(self):
        backend = InMemoryConfigBackend()
        await _save(backend, "orders", "prod", {"a": "specific"})
        await _save(backend, "orders", "default", {"a": "appdefault", "base": 1})
        await _save(backend, "application", "prod", {"shared": "p"})
        await _save(backend, "application", "default", {"shared": "base", "only": "x"})

        payload = await ConfigServer(backend).fetch("orders", "prod")
        assert payload is not None
        names = [ps["name"] for ps in payload["propertySources"]]
        assert names == ["orders-prod", "orders-default", "application-prod", "application-default"]

    @pytest.mark.asyncio
    async def test_missing_overlays_are_skipped(self):
        backend = InMemoryConfigBackend()
        await _save(backend, "orders", "prod", {"a": "b"})
        payload = await ConfigServer(backend).fetch("orders", "prod")
        assert payload is not None
        assert [ps["name"] for ps in payload["propertySources"]] == ["orders-prod"]

    @pytest.mark.asyncio
    async def test_default_profile_dedups(self):
        backend = InMemoryConfigBackend()
        await _save(backend, "orders", "default", {"a": "b"})
        await _save(backend, "application", "default", {"shared": "s"})
        payload = await ConfigServer(backend).fetch("orders", "default")
        assert payload is not None
        # (orders, default) appears once, not twice.
        assert [ps["name"] for ps in payload["propertySources"]] == ["orders-default", "application-default"]

    @pytest.mark.asyncio
    async def test_all_missing_returns_none(self):
        assert await ConfigServer(InMemoryConfigBackend()).fetch("absent", "x") is None


class TestBackendRoot:
    def test_uses_configured_root(self, tmp_path: pathlib.Path):
        cfg = Config({"pyfly": {"config-server": {"backend": {"root": str(tmp_path)}}}})
        backend = ConfigServerAutoConfiguration().config_backend(cfg)
        assert backend._root == pathlib.Path(tmp_path)

    @pytest.mark.asyncio
    async def test_configured_root_persists(self, tmp_path: pathlib.Path):
        cfg = Config({"pyfly": {"config-server": {"backend": {"root": str(tmp_path)}}}})
        backend = ConfigServerAutoConfiguration().config_backend(cfg)
        await backend.save(ConfigSource(application="svc", profile="dev", properties={"k": "v"}))
        # A fresh backend on the same root sees the persisted file.
        reopened = FilesystemConfigBackend(str(tmp_path))
        fetched = await reopened.fetch("svc", "dev")
        assert fetched is not None
        assert fetched.properties == {"k": "v"}

    def test_falls_back_to_tempdir(self):
        backend = ConfigServerAutoConfiguration().config_backend(Config({}))
        assert backend._root.name.startswith("pyfly-config-")
        assert backend._root.exists()
