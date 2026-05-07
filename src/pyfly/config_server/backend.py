# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Backend SPI + filesystem / in-memory adapters for the config server."""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ConfigSource:
    """One config bundle keyed by application + profile + (optional) label."""

    application: str
    profile: str
    label: str = "main"
    properties: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConfigBackend(Protocol):
    async def fetch(self, application: str, profile: str, label: str = "main") -> ConfigSource | None: ...
    async def save(self, source: ConfigSource) -> None: ...
    async def list(self) -> list[ConfigSource]: ...


class InMemoryConfigBackend:
    """Dict-backed config backend — perfect for tests."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], ConfigSource] = {}
        self._lock = asyncio.Lock()

    async def fetch(self, application: str, profile: str, label: str = "main") -> ConfigSource | None:
        async with self._lock:
            return self._store.get((application, profile, label))

    async def save(self, source: ConfigSource) -> None:
        async with self._lock:
            self._store[(source.application, source.profile, source.label)] = source

    async def list(self) -> list[ConfigSource]:
        async with self._lock:
            return list(self._store.values())


class FilesystemConfigBackend:
    """Loads config from ``<root>/<application>-<profile>.{yaml,yml,json}``.

    The label maps to a subdirectory: ``<root>/<label>/<application>-<profile>.yaml``.
    """

    def __init__(self, root: str | pathlib.Path) -> None:
        self._root = pathlib.Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_candidates(self, application: str, profile: str, label: str) -> list[pathlib.Path]:
        base = self._root / label if label else self._root
        return [
            base / f"{application}-{profile}.yaml",
            base / f"{application}-{profile}.yml",
            base / f"{application}-{profile}.json",
            self._root / f"{application}-{profile}.yaml",
            self._root / f"{application}-{profile}.yml",
            self._root / f"{application}-{profile}.json",
        ]

    async def fetch(self, application: str, profile: str, label: str = "main") -> ConfigSource | None:
        for candidate in self._path_candidates(application, profile, label):
            if not candidate.exists():
                continue
            text = await asyncio.get_event_loop().run_in_executor(None, candidate.read_text)
            properties = await asyncio.get_event_loop().run_in_executor(
                None, _parse_text, text, candidate.suffix.lstrip(".")
            )
            return ConfigSource(
                application=application, profile=profile, label=label, properties=properties
            )
        return None

    async def save(self, source: ConfigSource) -> None:
        import json

        target = self._root / source.label if source.label else self._root
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{source.application}-{source.profile}.json"
        await asyncio.get_event_loop().run_in_executor(
            None, path.write_text, json.dumps(source.properties, indent=2)
        )

    async def list(self) -> list[ConfigSource]:
        results: list[ConfigSource] = []
        for path in self._root.rglob("*"):
            if not path.is_file() or path.suffix.lstrip(".") not in {"yaml", "yml", "json"}:
                continue
            try:
                stem = path.stem
                if "-" not in stem:
                    continue
                application, _, profile = stem.partition("-")
                label = path.parent.name if path.parent != self._root else "main"
                source = await self.fetch(application, profile, label)
                if source is not None:
                    results.append(source)
            except Exception:  # noqa: BLE001
                continue
        return results


def _parse_text(text: str, fmt: str) -> dict[str, Any]:
    if fmt == "json":
        import json

        return json.loads(text)
    import yaml

    return yaml.safe_load(text) or {}
