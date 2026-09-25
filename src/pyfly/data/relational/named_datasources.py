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
              url: "postgresql+asyncpg://app:${REPORTING_PASSWORD}@.../reporting"
              pool:
                size: 3

Inject :class:`NamedDataSources` and call ``.get("reporting")`` for that datasource's
``async_sessionmaker``. The primary datasource keeps its own dedicated beans.

The ``named_data_sources`` bean is a live view over the
:class:`~pyfly.data.relational.datasource_registry.DataSourceRegistry`, which builds each named
datasource with the primary's treatment (pool, connect arguments, SQLite setup) overridden by its own
keys, resolves ``${...}`` placeholders and ``PYFLY_*`` overrides in every key, and disposes it when the
context stops.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyfly.config.properties.data import PRIMARY, RelationalProperties

if TYPE_CHECKING:
    from pyfly.data.relational.datasource_registry import DataSourceRegistry


class NamedDataSources:
    """Registry of named secondary datasource session factories.

    Build it over a :class:`~pyfly.data.relational.datasource_registry.DataSourceRegistry` with
    :meth:`of_registry` (what the auto-configuration does), or from explicit ``factories`` (and the
    ``engines`` :meth:`dispose` closes).
    """

    def __init__(
        self,
        factories: dict[str, Any] | None = None,
        engines: dict[str, Any] | None = None,
        *,
        registry: DataSourceRegistry | None = None,
    ) -> None:
        self._factories = dict(factories or {})
        self._engines = dict(engines or {})
        self._registry = registry

    @classmethod
    def of_registry(cls, registry: DataSourceRegistry) -> NamedDataSources:
        """A live view over *registry*'s datasources other than the primary."""
        return cls(registry=registry)

    def get(self, name: str) -> Any:
        """Return the ``async_sessionmaker`` for *name* (raises ``KeyError`` if unknown)."""
        if self._registry is not None and name != PRIMARY and name in self._registry.names():
            return self._registry.get(name).sessionmaker
        try:
            return self._factories[name]
        except KeyError:
            raise KeyError(f"No datasource named {name!r}; configured: {self.names()}") from None

    def names(self) -> list[str]:
        """Sorted names of all configured secondary datasources."""
        names = set(self._factories)
        if self._registry is not None:
            names.update(name for name in self._registry.names() if name != PRIMARY)
        return sorted(names)

    def __contains__(self, name: object) -> bool:
        return name in self.names()

    def __len__(self) -> int:
        return len(self.names())

    async def dispose(self) -> None:
        """Dispose every secondary engine (the registry does this on shutdown for its own)."""
        for engine in self._engines.values():
            await engine.dispose()
        if self._registry is not None:
            for name in self.names():
                if name in self._registry.names():
                    await self._registry.get(name).dispose()


def build_named_data_sources(config: Any, engine_factory: Any, session_factory: Any) -> NamedDataSources:
    """Build a :class:`NamedDataSources` from ``pyfly.data.relational.datasources.*`` config.

    *engine_factory* is called ``engine_factory(url, echo=...)`` and *session_factory* is
    called ``session_factory(engine)`` — injected so this stays free of a hard SQLAlchemy
    import (the relational extra owns those). The settings are read like the registry reads them:
    placeholders resolve, ``PYFLY_*`` overrides win, and ``echo`` is a real boolean. The auto-configured
    ``named_data_sources`` bean does not use this helper; it is a view over the registry.
    """
    factories: dict[str, Any] = {}
    engines: dict[str, Any] = {}
    for name, settings in RelationalProperties.from_config(config).datasources.items():
        engine = engine_factory(str(settings.url), echo=settings.echo)
        engines[name] = engine
        factories[name] = session_factory(engine)
    return NamedDataSources(factories, engines)
