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
"""The ``file`` layer: a flagd document (``.json``, ``.yaml``, ``.yml``) reloaded when its mtime or size changes.

YAML is read with YAML 1.2 booleans (only ``true``/``false``), as flagd reads its own files, so ``on``/``off``
variant names stay names instead of turning into booleans as PyYAML's default YAML 1.1 rules would make them.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from pyfly.feature_flags.definitions import FlagDocument, parse_document
from pyfly.feature_flags.sources import SourceSnapshot

__all__ = ["FileFlagSource"]

_SUFFIXES = (".json", ".yaml", ".yml")
_BOOL_TAG = "tag:yaml.org,2002:bool"


class _Yaml12Loader(yaml.SafeLoader):  # type: ignore[misc]
    """``SafeLoader`` whose booleans are YAML 1.2's: ``on``, ``off``, ``yes`` and ``no`` stay strings."""


_Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != _BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(_BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


class FileFlagSource:
    """A watched flagd document. A failed load is never remembered, so the next poll tries again."""

    name = "file"
    fail_fast = True

    def __init__(self, path: str | Path, *, refresh_interval: float = 5.0) -> None:
        self._path = Path(path)
        if self._path.suffix.lower() not in _SUFFIXES:
            raise ValueError(f"pyfly.feature-flags.sources.file.path must name a .json, .yaml or .yml file: {path}")
        self.refresh_interval: float | None = refresh_interval
        self._signature: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> SourceSnapshot | None:
        read = await asyncio.to_thread(self._read)  # a stalled mount must not freeze the event loop
        if read is None:
            return None
        signature, document = read
        self._signature = signature  # only after the document validated
        return SourceSnapshot(document, revision=f"{signature[0]}:{signature[1]}")

    def _read(self) -> tuple[tuple[int, int], FlagDocument] | None:
        """The blocking part, run in a worker thread: stat, read, parse and validate. ``None`` when unchanged."""
        stat = self._path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._signature:
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
            raw: Any = (
                json.loads(text) if self._path.suffix.lower() == ".json" else yaml.load(text, Loader=_Yaml12Loader)
            )
        except (ValueError, yaml.YAMLError) as error:  # bad JSON, bad YAML or bad UTF-8: say which file
            raise ValueError(f"{self._path}: {error}") from error
        document = parse_document({} if raw is None else raw)  # only an absent document is empty
        return signature, document

    async def close(self) -> None:
        return None
