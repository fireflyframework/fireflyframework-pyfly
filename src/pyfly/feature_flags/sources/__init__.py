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
"""Flag sources: the layers the registry composes (spec 4.5), lowest precedence first: config, file, http, store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pyfly.feature_flags.definitions import FlagDocument

__all__ = ["FlagSource", "FlagSourceError", "SourceLoadDeferred", "SourceSnapshot"]


@dataclass(frozen=True)
class SourceSnapshot:
    """A successfully loaded and validated document, and its revision (``None`` when the source has none)."""

    document: FlagDocument
    revision: str | None = None


class SourceLoadDeferred(Exception):
    """No load was attempted; preserve the source's prior document and health until a later refresh."""


class FlagSourceError(RuntimeError):
    """A source that must load at startup (``fail_fast``: config, file) could not; startup fails with it."""

    def __init__(self, source: str, cause: BaseException) -> None:
        super().__init__(f"feature flag source {source!r} failed to load: {cause}")
        self.source = source


@runtime_checkable
class FlagSource(Protocol):
    """One layer of the composition.

    ``load()`` returns the source's current document, or ``None`` when nothing changed since its last successful
    load. ``SourceLoadDeferred`` skips a load without changing document or health; other exceptions report
    failure (the registry keeps the last good document). ``refresh_interval`` is the
    seconds between polls (``None``: loaded once, at startup).
    """

    @property
    def name(self) -> str: ...

    @property
    def fail_fast(self) -> bool: ...

    @property
    def refresh_interval(self) -> float | None: ...

    async def load(self) -> SourceSnapshot | None: ...

    async def close(self) -> None: ...
