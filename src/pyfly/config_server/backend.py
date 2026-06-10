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

    **Tiered search locations**

    Pass *search_locations* (a list of directory paths, highest-precedence
    first) to merge config from multiple directories.  The convention is::

        search_locations=[domain_dir, core_dir, common_dir]

    so the domain layer overrides core which overrides common.  Keys that exist
    only in a lower-precedence location are inherited (fill-in semantics).
    ``save()`` and ``list()`` operate on the **first** (primary / highest-
    precedence) location; the single-root behaviour is unchanged when
    *search_locations* is ``None``.
    """

    def __init__(
        self,
        root: str | pathlib.Path,
        *,
        search_locations: list[str | pathlib.Path] | None = None,
    ) -> None:
        self._root = pathlib.Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        if search_locations is not None:
            # The primary location (index 0 = highest precedence) is the caller-
            # supplied root; remaining locations follow in order.
            self._locations: list[pathlib.Path] = [pathlib.Path(p) for p in search_locations]
            for loc in self._locations:
                loc.mkdir(parents=True, exist_ok=True)
        else:
            self._locations = []

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

    def _path_candidates_for(
        self, root: pathlib.Path, application: str, profile: str, label: str
    ) -> list[pathlib.Path]:
        """Like ``_path_candidates`` but for an arbitrary *root*."""
        base = root / label if label else root
        return [
            base / f"{application}-{profile}.yaml",
            base / f"{application}-{profile}.yml",
            base / f"{application}-{profile}.json",
            root / f"{application}-{profile}.yaml",
            root / f"{application}-{profile}.yml",
            root / f"{application}-{profile}.json",
        ]

    async def _fetch_from_root(
        self, root: pathlib.Path, application: str, profile: str, label: str
    ) -> ConfigSource | None:
        """Attempt to read a single file match from *root*."""
        for candidate in self._path_candidates_for(root, application, profile, label):
            if not candidate.exists():
                continue
            text = await asyncio.get_running_loop().run_in_executor(None, candidate.read_text)
            properties = await asyncio.get_running_loop().run_in_executor(
                None, _parse_text, text, candidate.suffix.lstrip(".")
            )
            return ConfigSource(application=application, profile=profile, label=label, properties=properties)
        return None

    async def fetch(self, application: str, profile: str, label: str = "main") -> ConfigSource | None:
        if self._locations:
            # Tiered mode: iterate locations from lowest to highest precedence
            # and accumulate properties, so higher-precedence locations win.
            merged: dict[str, Any] = {}
            found = False
            for loc in reversed(self._locations):
                source = await self._fetch_from_root(loc, application, profile, label)
                if source is not None:
                    merged.update(source.properties)
                    found = True
            if not found:
                return None
            return ConfigSource(
                application=application,
                profile=profile,
                label=label,
                properties=merged,
            )

        # Single-root mode (original behaviour).
        for candidate in self._path_candidates(application, profile, label):
            if not candidate.exists():
                continue
            text = await asyncio.get_running_loop().run_in_executor(None, candidate.read_text)
            properties = await asyncio.get_running_loop().run_in_executor(
                None, _parse_text, text, candidate.suffix.lstrip(".")
            )
            return ConfigSource(application=application, profile=profile, label=label, properties=properties)
        return None

    async def save(self, source: ConfigSource) -> None:
        import json

        # Always write to _root (the primary / highest-precedence location).
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
        await asyncio.get_running_loop().run_in_executor(None, path.write_text, text)

        # Guarantee exactly one file backs this (application, profile, label) so
        # future fetches/saves can't diverge across stale duplicate formats.
        for other in existing:
            if other != path:
                other.unlink(missing_ok=True)

    async def list(self) -> list[ConfigSource]:
        # Always list from _root (the primary location).
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
