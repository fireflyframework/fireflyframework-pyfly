# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Backend SPI + filesystem / in-memory adapters for the config server."""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import yaml  # type: ignore[import-untyped]


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
            return ConfigSource(application=application, profile=profile, label=label, properties=properties)
        return None

    async def save(self, source: ConfigSource) -> None:
        import json

        candidates = self._path_candidates(source.application, source.profile, source.label)
        existing = [c for c in candidates if c.exists()]

        # Write back to the SAME file fetch() would read (highest-priority
        # existing candidate), preserving its format — otherwise a save that
        # wrote a fresh .json would be silently shadowed by a pre-existing
        # higher-priority .yaml. If none exists yet, create a .json.
        if existing:
            path = existing[0]
        else:
            target = self._root / source.label if source.label else self._root
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{source.application}-{source.profile}.json"

        if path.suffix.lstrip(".") in {"yaml", "yml"}:
            text = yaml.safe_dump(source.properties, sort_keys=False)
        else:
            text = json.dumps(source.properties, indent=2)

        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.get_event_loop().run_in_executor(None, path.write_text, text)

        # Guarantee exactly one file backs this (application, profile, label) so
        # future fetches/saves can't diverge across stale duplicate formats.
        for other in existing:
            if other != path:
                other.unlink(missing_ok=True)

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

        result: dict[str, Any] = json.loads(text)
        return result

    parsed: dict[str, Any] | None = yaml.safe_load(text)
    return parsed or {}
