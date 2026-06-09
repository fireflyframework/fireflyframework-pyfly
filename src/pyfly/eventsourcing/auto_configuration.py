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

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eventsourcing.publisher import EventSourcingPublisher
from pyfly.eventsourcing.snapshot import InMemorySnapshotStore, SnapshotStore, SqlAlchemySnapshotStore
from pyfly.eventsourcing.store import EventStore, InMemoryEventStore, SqlAlchemyEventStore


@auto_configuration
@conditional_on_property("pyfly.eventsourcing.enabled", having_value="true")
class EventSourcingAutoConfiguration:
    """Wire event-sourcing beans into the DI container.

    * ``event_store`` — ``memory`` (default) or ``sqlalchemy``
      (config: ``pyfly.eventsourcing.store.provider``).
    * ``snapshot_store`` — ``memory`` (default) or ``sqlalchemy``
      (config: ``pyfly.eventsourcing.snapshot.provider``).
    * ``event_sourcing_publisher`` — bridges stored events onto the EDA bus;
      only wired when an :class:`~pyfly.eda.ports.outbound.EventPublisher` bean
      is present (config: ``pyfly.eventsourcing.eda.destination``).
    """

    @bean
    def event_store(self, config: Config) -> EventStore:
        provider = str(config.get("pyfly.eventsourcing.store.provider", "memory")).lower()
        if provider == "memory":
            return InMemoryEventStore()
        if provider == "sqlalchemy":
            try:
                from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore[import-not-found, unused-ignore]
            except ImportError as exc:
                raise ValueError(
                    "pyfly.eventsourcing.store.provider=sqlalchemy requires the 'sqlalchemy' "
                    "and 'aiosqlite' / 'asyncpg' extras to be installed."
                ) from exc
            url = str(
                config.get("pyfly.eventsourcing.store.url")
                or config.get("pyfly.data.relational.url", "sqlite+aiosqlite:///./app.db")
            )
            engine = create_async_engine(url, echo=False)
            return SqlAlchemyEventStore(engine)
        raise ValueError(f"Unknown pyfly.eventsourcing.store.provider={provider!r}. Valid values: memory, sqlalchemy.")

    @bean
    def snapshot_store(self, config: Config) -> SnapshotStore:
        provider = str(config.get("pyfly.eventsourcing.snapshot.provider", "memory")).lower()
        if provider == "memory":
            return InMemorySnapshotStore()
        if provider == "sqlalchemy":
            try:
                from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore[import-not-found, unused-ignore]
            except ImportError as exc:
                raise ValueError(
                    "pyfly.eventsourcing.snapshot.provider=sqlalchemy requires the 'sqlalchemy' "
                    "and 'aiosqlite' / 'asyncpg' extras to be installed."
                ) from exc
            url = str(
                config.get("pyfly.eventsourcing.snapshot.url")
                or config.get("pyfly.data.relational.url", "sqlite+aiosqlite:///./app.db")
            )
            engine = create_async_engine(url, echo=False)
            return SqlAlchemySnapshotStore(engine)
        raise ValueError(
            f"Unknown pyfly.eventsourcing.snapshot.provider={provider!r}. Valid values: memory, sqlalchemy."
        )

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
