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
"""Additional named datasources (Spring's multiple ``DataSource`` beans).

Configure extra datasources under ``pyfly.data.relational.datasources.<name>``::

    pyfly:
      data:
        relational:
          url: "postgresql+asyncpg://.../primary"   # the primary (unchanged)
          datasources:
            reporting:
              url: "postgresql+asyncpg://.../reporting"
              echo: false

Inject :class:`NamedDataSources` and call ``.get("reporting")`` for that datasource's
``async_sessionmaker``. The primary datasource keeps its own dedicated beans.
"""

from __future__ import annotations

from typing import Any


class NamedDataSources:
    """Registry of named secondary datasource session factories."""

    def __init__(self, factories: dict[str, Any], engines: dict[str, Any] | None = None) -> None:
        self._factories = dict(factories)
        self._engines = dict(engines or {})

    def get(self, name: str) -> Any:
        """Return the ``async_sessionmaker`` for *name* (raises ``KeyError`` if unknown)."""
        try:
            return self._factories[name]
        except KeyError:
            raise KeyError(f"No datasource named {name!r}; configured: {self.names()}") from None

    def names(self) -> list[str]:
        """Sorted names of all configured secondary datasources."""
        return sorted(self._factories)

    def __contains__(self, name: object) -> bool:
        return name in self._factories

    def __len__(self) -> int:
        return len(self._factories)

    async def dispose(self) -> None:
        """Dispose every secondary engine (call on shutdown)."""
        for engine in self._engines.values():
            await engine.dispose()


def build_named_data_sources(config: Any, engine_factory: Any, session_factory: Any) -> NamedDataSources:
    """Build a :class:`NamedDataSources` from ``pyfly.data.relational.datasources.*`` config.

    *engine_factory* is called ``engine_factory(url, echo=...)`` and *session_factory* is
    called ``session_factory(engine)`` — injected so this stays free of a hard SQLAlchemy
    import (the relational extra owns those).
    """
    raw = config.get("pyfly.data.relational.datasources") or {}
    factories: dict[str, Any] = {}
    engines: dict[str, Any] = {}
    if isinstance(raw, dict):
        for name, settings in raw.items():
            if not isinstance(settings, dict):
                continue
            url = settings.get("url")
            if not url:
                continue
            engine = engine_factory(str(url), echo=bool(settings.get("echo", False)))
            engines[name] = engine
            factories[name] = session_factory(engine)
    return NamedDataSources(factories, engines)
