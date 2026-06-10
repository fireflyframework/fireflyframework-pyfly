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
"""Tests for FilesystemConfigBackend tiered search_locations overlay.

Convention: pass locations highest-precedence first, e.g.::

    FilesystemConfigBackend(domain, search_locations=[domain, core, common])

Higher-precedence locations override lower; keys present only in a lower
location are inherited (fill-in semantics).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from pyfly.config_server.backend import ConfigSource, FilesystemConfigBackend


def _write(directory: pathlib.Path, app: str, profile: str, data: dict) -> None:
    """Write a YAML config file at <directory>/<app>-<profile>.yaml."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{app}-{profile}.yaml").write_text(yaml.safe_dump(data))


@pytest.mark.asyncio
async def test_domain_overrides_common(tmp_path: pathlib.Path) -> None:
    """A key present in both domain and common takes the domain value."""
    common = tmp_path / "common"
    domain = tmp_path / "domain"
    _write(common, "orders", "prod", {"host": "common.db", "timeout": 5, "common_only": "yes"})
    _write(domain, "orders", "prod", {"host": "domain.db", "domain_only": "yes"})

    backend = FilesystemConfigBackend(domain, search_locations=[domain, common])
    source = await backend.fetch("orders", "prod")

    assert source is not None
    assert source.properties["host"] == "domain.db", "domain must override common"
    assert source.properties["timeout"] == 5, "common-only key must be inherited"
    assert source.properties["common_only"] == "yes", "common-only key must be inherited"
    assert source.properties["domain_only"] == "yes", "domain-only key must be present"


@pytest.mark.asyncio
async def test_three_tier_override_chain(tmp_path: pathlib.Path) -> None:
    """Three tiers: domain > core > common."""
    common = tmp_path / "common"
    core = tmp_path / "core"
    domain = tmp_path / "domain"

    _write(common, "svc", "default", {"log_level": "INFO", "timeout": 30, "common_key": "c"})
    _write(core, "svc", "default", {"log_level": "WARN", "core_key": "k"})
    _write(domain, "svc", "default", {"log_level": "DEBUG", "domain_key": "d"})

    backend = FilesystemConfigBackend(domain, search_locations=[domain, core, common])
    source = await backend.fetch("svc", "default")

    assert source is not None
    # Highest precedence wins.
    assert source.properties["log_level"] == "DEBUG"
    # All layers contribute unique keys.
    assert source.properties["timeout"] == 30
    assert source.properties["common_key"] == "c"
    assert source.properties["core_key"] == "k"
    assert source.properties["domain_key"] == "d"


@pytest.mark.asyncio
async def test_missing_in_all_locations_returns_none(tmp_path: pathlib.Path) -> None:
    """fetch() returns None when no location has a matching file."""
    common = tmp_path / "common"
    domain = tmp_path / "domain"
    common.mkdir()
    domain.mkdir()

    backend = FilesystemConfigBackend(domain, search_locations=[domain, common])
    result = await backend.fetch("missing", "dev")
    assert result is None


@pytest.mark.asyncio
async def test_partial_presence_only_in_lower(tmp_path: pathlib.Path) -> None:
    """A file that exists only in a lower-precedence location is still returned."""
    common = tmp_path / "common"
    domain = tmp_path / "domain"
    domain.mkdir()
    _write(common, "shared", "default", {"base_url": "http://common"})

    backend = FilesystemConfigBackend(domain, search_locations=[domain, common])
    source = await backend.fetch("shared", "default")

    assert source is not None
    assert source.properties["base_url"] == "http://common"


@pytest.mark.asyncio
async def test_save_writes_to_primary_location(tmp_path: pathlib.Path) -> None:
    """save() writes to the primary (first / highest-precedence) location."""
    common = tmp_path / "common"
    domain = tmp_path / "domain"
    common.mkdir()
    domain.mkdir()

    backend = FilesystemConfigBackend(domain, search_locations=[domain, common])
    await backend.save(ConfigSource(application="svc", profile="prod", properties={"key": "val"}))

    # File must appear in domain, not common.
    matches = list(domain.rglob("svc-prod.*"))
    assert matches, "file should be written to domain (primary) location"
    common_matches = list(common.rglob("svc-prod.*"))
    assert not common_matches, "file must NOT be written to common"


@pytest.mark.asyncio
async def test_list_uses_primary_location(tmp_path: pathlib.Path) -> None:
    """list() only enumerates files in the primary location."""
    common = tmp_path / "common"
    domain = tmp_path / "domain"
    _write(common, "shared", "default", {"x": 1})
    _write(domain, "orders", "prod", {"y": 2})

    backend = FilesystemConfigBackend(domain, search_locations=[domain, common])
    sources = await backend.list()

    apps = {s.application for s in sources}
    assert "orders" in apps
    # "shared" lives only in common; list() must not return it.
    assert "shared" not in apps


@pytest.mark.asyncio
async def test_single_root_unchanged(tmp_path: pathlib.Path) -> None:
    """Existing single-root behaviour is intact when search_locations is None."""
    _write(tmp_path, "orders", "dev", {"db": "sqlite"})

    backend = FilesystemConfigBackend(tmp_path)
    source = await backend.fetch("orders", "dev")

    assert source is not None
    assert source.properties["db"] == "sqlite"
