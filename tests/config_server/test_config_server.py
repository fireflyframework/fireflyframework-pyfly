# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the config-server module."""

from __future__ import annotations

import pathlib

import pytest

from pyfly.config_server.backend import (
    ConfigSource,
    FilesystemConfigBackend,
    InMemoryConfigBackend,
)
from pyfly.config_server.server import ConfigServer


@pytest.mark.asyncio
async def test_in_memory_round_trip() -> None:
    backend = InMemoryConfigBackend()
    await backend.save(ConfigSource(application="orders", profile="prod", properties={"x": 1}))
    fetched = await backend.fetch("orders", "prod")
    assert fetched is not None
    assert fetched.properties == {"x": 1}


@pytest.mark.asyncio
async def test_filesystem_save_updates_existing_yaml(tmp_path: pathlib.Path) -> None:
    """Regression: save() must update the file fetch() reads (a pre-existing
    .yaml), not write a shadowed .json that fetch ignores."""
    import yaml

    yaml_file = tmp_path / "main" / "orders-prod.yaml"
    yaml_file.parent.mkdir(parents=True, exist_ok=True)
    yaml_file.write_text(yaml.safe_dump({"v": "old"}))

    backend = FilesystemConfigBackend(tmp_path)
    await backend.save(ConfigSource(application="orders", profile="prod", label="main", properties={"v": "new"}))

    fetched = await backend.fetch("orders", "prod", "main")
    assert fetched is not None
    assert fetched.properties == {"v": "new"}  # not the stale "old"
    # And no shadow .json was created alongside the .yaml.
    assert not (tmp_path / "main" / "orders-prod.json").exists()


@pytest.mark.asyncio
async def test_filesystem_round_trip(tmp_path: pathlib.Path) -> None:
    backend = FilesystemConfigBackend(tmp_path)
    await backend.save(ConfigSource(application="orders", profile="dev", properties={"y": "v"}))
    fetched = await backend.fetch("orders", "dev")
    assert fetched is not None
    assert fetched.properties == {"y": "v"}
    listed = await backend.list()
    assert any(s.application == "orders" for s in listed)


@pytest.mark.asyncio
async def test_server_returns_property_sources() -> None:
    backend = InMemoryConfigBackend()
    await backend.save(ConfigSource(application="orders", profile="prod", properties={"a": "b"}))
    server = ConfigServer(backend=backend)
    payload = await server.fetch("orders", "prod")
    assert payload is not None
    assert payload["name"] == "orders"
    assert payload["propertySources"][0]["source"] == {"a": "b"}
