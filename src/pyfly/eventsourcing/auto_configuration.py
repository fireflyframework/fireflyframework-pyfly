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
"""Auto-configuration for the event-sourcing module."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

from typing import Any

from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eventsourcing.checkpoint import CheckpointStore, InMemoryCheckpointStore, SqlAlchemyCheckpointStore
from pyfly.eventsourcing.publisher import EventSourcingPublisher
from pyfly.eventsourcing.snapshot import InMemorySnapshotStore, SnapshotStore, SqlAlchemySnapshotStore
from pyfly.eventsourcing.store import EventStore, InMemoryEventStore, SqlAlchemyEventStore

_STORE = "pyfly.eventsourcing.store"
_SNAPSHOT = "pyfly.eventsourcing.snapshot"
_CHECKPOINT = "pyfly.eventsourcing.projection.checkpoint"


def _provider(config: Config, prefix: str, default: str = "memory") -> str:
    return str(config.get(f"{prefix}.provider", default) or default).strip().lower()


def _configured(config: Config, key: str) -> bool:
    return bool(str(config.get(key, "") or "").strip())


def _has_datasource(config: Config, prefix: str) -> bool:
    """Whether the store under *prefix* names its datasource (``<prefix>.datasource``) or gives its URL."""
    return _configured(config, f"{prefix}.datasource") or _configured(config, f"{prefix}.url")


def _datasource(
    config: Config, container: Container | None, prefix: str, *, name: str, fallback: tuple[str, str] | None = None
) -> tuple[Any, bool]:
    """The datasource of the SQL store configured under *prefix*, resolved in the context's registry, and whether
    the store creates its tables (``pyfly.data.relational.ddl-auto``).

    *fallback* is the ``(prefix, name)`` of the store whose datasource this one takes when neither
    ``<prefix>.datasource`` nor ``<prefix>.url`` is set, that store has one of them, and there is no primary
    datasource."""
    try:
        from pyfly.data.relational.framework_schema import (
            context_datasource_registry,
            creates_tables,
            module_datasource,
        )
    except ImportError as exc:
        raise ValueError(
            f"{prefix}.provider=sqlalchemy requires the 'sqlalchemy' and 'aiosqlite' / 'asyncpg' extras to be "
            "installed."
        ) from exc
    # <prefix>.datasource names a datasource; <prefix>.url is an alias resolved through the context's registry: no
    # URL is the primary datasource, an identical URL reuses that datasource's engine, another URL registers *name*.
    registry = context_datasource_registry(config, container)
    create = creates_tables(registry.properties.ddl_auto)
    if (
        fallback is not None
        and not _has_datasource(config, prefix)
        and _has_datasource(config, fallback[0])
        and not registry.has_primary
    ):
        return module_datasource(registry, config, fallback[0], name=fallback[1]), create
    return module_datasource(registry, config, prefix, name=name), create


@auto_configuration
@conditional_on_property("pyfly.eventsourcing.enabled", having_value="true")
class EventSourcingAutoConfiguration:
    """Wire event-sourcing beans into the DI container.

    * ``event_store`` — ``memory`` (default) or ``sqlalchemy``
      (config: ``pyfly.eventsourcing.store.provider``).
    * ``snapshot_store`` — ``memory`` (default) or ``sqlalchemy``
      (config: ``pyfly.eventsourcing.snapshot.provider``).
    * ``projection_checkpoint_store`` — ``memory`` or ``sqlalchemy``; by default the event store's provider
      (config: ``pyfly.eventsourcing.projection.checkpoint.provider``).
    * ``event_sourcing_publisher`` — bridges stored events onto the EDA bus;
      only wired when an :class:`~pyfly.eda.ports.outbound.EventPublisher` bean
      is present (config: ``pyfly.eventsourcing.eda.destination``).

    A SQL store runs on the datasource named by ``<prefix>.datasource``, or the one whose URL is ``<prefix>.url``,
    or the primary, looked up in the context's datasource registry; with none of them and no primary, the
    checkpoints go with the event store. A store creates its tables at start when
    ``pyfly.data.relational.ddl-auto`` allows it and checks them either way
    (:func:`~pyfly.data.relational.framework_schema.ensure_tables`); the checkpoint store the event store's
    provider brings along (no ``pyfly.eventsourcing.projection.checkpoint.provider``) does both on first use
    instead, so an application with no projection needs neither of its tables.
    """

    @bean
    def event_store(self, config: Config, container: Container | None = None) -> EventStore:
        provider = _provider(config, _STORE)
        if provider == "memory":
            return InMemoryEventStore()
        if provider == "sqlalchemy":
            datasource, create = _datasource(config, container, _STORE, name="event-store")
            strategy = str(config.get(f"{_STORE}.position-strategy", "auto") or "auto").strip().lower()
            return SqlAlchemyEventStore(datasource, create_table=create, position_strategy=strategy)
        raise ValueError(f"Unknown {_STORE}.provider={provider!r}. Valid values: memory, sqlalchemy.")

    @bean
    def snapshot_store(self, config: Config, container: Container | None = None) -> SnapshotStore:
        provider = _provider(config, _SNAPSHOT)
        if provider == "memory":
            return InMemorySnapshotStore()
        if provider == "sqlalchemy":
            datasource, create = _datasource(config, container, _SNAPSHOT, name="snapshot-store")
            return SqlAlchemySnapshotStore(datasource, create_table=create)
        raise ValueError(f"Unknown {_SNAPSHOT}.provider={provider!r}. Valid values: memory, sqlalchemy.")

    @bean
    def projection_checkpoint_store(self, config: Config, container: Container | None = None) -> CheckpointStore:
        """Where ``ProjectionRunner`` keeps each projection's position: pass it as ``checkpoints=``. Put it on the
        read models' datasource (``pyfly.eventsourcing.projection.checkpoint.datasource``): a batch's read-model
        writes and its checkpoint then commit together.

        Without a datasource or URL of its own it is on the primary, or on the event store's datasource when there
        is no primary. When its provider follows the event store's (not set), it creates or checks its tables on
        first use rather than at start."""
        explicit = _configured(config, f"{_CHECKPOINT}.provider")
        provider = _provider(config, _CHECKPOINT, default=_provider(config, _STORE))
        if provider == "memory":
            return InMemoryCheckpointStore()
        if provider == "sqlalchemy":
            datasource, create = _datasource(
                config, container, _CHECKPOINT, name="projection-checkpoints", fallback=(_STORE, "event-store")
            )
            return SqlAlchemyCheckpointStore(datasource, create_table=create, check_at_start=explicit)
        raise ValueError(f"Unknown {_CHECKPOINT}.provider={provider!r}. Valid values: memory, sqlalchemy.")

    @bean
    def event_sourcing_publisher(
        self, config: Config, event_publisher: EventPublisher | None = None
    ) -> EventSourcingPublisher | None:
        """Bridge stored events onto the EDA bus.

        Created only when an :class:`~pyfly.eda.ports.outbound.EventPublisher`
        bean is present (i.e. when an EDA adapter is active).  When no publisher
        is available this bean returns ``None`` and is skipped by the lifecycle
        machinery — matching the optional-bean pattern used throughout PyFly.
        """
        if event_publisher is None:
            return None
        destination = str(config.get("pyfly.eventsourcing.eda.destination", "pyfly.events"))
        return EventSourcingPublisher(event_publisher, destination=destination)
