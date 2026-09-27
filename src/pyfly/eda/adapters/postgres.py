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
"""Postgres-backed ``EventPublisher`` (``pyfly.eda.provider=postgres``): the transactional outbox with
LISTEN/NOTIFY wake-ups.

:class:`PostgresEventBus` is the :class:`~pyfly.eda.adapters.database.DatabaseEventBus` with the constructor
this adapter always had. It runs on a datasource of the application's ``DataSourceRegistry`` (the
auto-configuration passes it; ``pyfly.eda.postgres.dsn`` is an alias of the datasource with that URL), with
that datasource's pool, connect arguments and dialect setup, and holds no connection pool of its own.

Delivery
========

* A publish writes the event into ``pyfly_outbox_events`` **in the publisher's unit of work**, and owes it to
  every consumer group registered for its destination: a rolled-back unit publishes nothing, a committed one
  cannot lose its event. On PostgreSQL it is one statement, ``NOTIFY`` included (the server delivers the
  notification only if the unit commits).
* Consumers **claim delivery rows by state** (``FOR UPDATE SKIP LOCKED``): an event whose transaction commits
  after a later one's is claimed when it becomes visible. The id cursor of earlier releases skipped it for
  good (C009/C010).
* **At-least-once, per subscription**: a handler that fails is attempted again after a back-off, and after the
  last attempt its event is copied into ``pyfly_outbox_dead_letters``; the other handlers of the group, and the
  group's other events, go on meanwhile (C064).
* The ``LISTEN`` connection only wakes the relay: the relay also polls every ``poll_interval_s`` seconds, and a
  lost connection is reopened (and reported by the health indicator meanwhile).

Pgbouncer
=========

This adapter holds a long-lived ``LISTEN`` connection, checked out of the datasource's pool. Behind pgbouncer
in transaction-pooling mode, pass ``listen_dsn``: a direct connection to the server (session pooling is fine).

Requires ``asyncpg`` (``pip install pyfly[postgresql]`` or ``pip install pyfly[eda]``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyfly.eda.adapters.database import DatabaseEventBus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyfly.data.relational.datasource_registry import DataSourceRegistry


def _normalise_dsn(dsn: str) -> str:
    """Strip SQLAlchemy dialect markers so asyncpg can parse the URL."""
    for marker in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres+asyncpg://"):
        if dsn.startswith(marker):
            return "postgresql://" + dsn[len(marker) :]
    return dsn


def _sqlalchemy_url(dsn: str) -> str:
    """*dsn* as an asyncpg SQLAlchemy URL (``postgresql://`` would pick psycopg on SQLAlchemy 2.1)."""
    for scheme in ("postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return "postgresql+asyncpg://" + dsn[len(scheme) :]
    return dsn


class PostgresEventBus(DatabaseEventBus):
    """``EventPublisher`` on the transactional outbox, with PostgreSQL's LISTEN/NOTIFY as its wake-up.

    Name the datasource with *datasource* (a name, a ``DataSource``, an ``AsyncEngine``), or give its URL as
    *dsn*: the datasource of the running application context with that URL is used, and without one the bus
    builds a registry of its own for it (with the framework's pool and dialect setup), closed when it stops.
    With neither, the default datasource. *listen_dsn* is a direct URL for the ``LISTEN`` connection, *channel*
    the notification channel, *auto_create_tables* whether missing outbox tables are created. Every other
    option is :class:`~pyfly.eda.adapters.database.DatabaseEventBus`'s. A publish after :meth:`stop` on a bus
    that built its own registry builds it for that publish alone, and closes it again.
    """

    def __init__(
        self,
        *,
        dsn: str | None = None,
        datasource: object = None,
        listen_dsn: str | None = None,
        channel: str = "pyfly_eda",
        destinations: Sequence[str] | None = None,
        group: str = "default",
        poll_interval_s: float = 5.0,
        auto_create_tables: bool = True,
        **options: Any,
    ) -> None:
        if dsn and datasource is not None:
            raise ValueError("PostgresEventBus takes a dsn or a datasource, not both")
        self._dsn = _sqlalchemy_url(dsn) if dsn else None
        self._own_registry: DataSourceRegistry | None = None
        super().__init__(
            datasource,
            destinations=destinations,
            group=group,
            poll_interval=poll_interval_s,
            create_tables=auto_create_tables,
            channel=channel,
            listen_dsn=listen_dsn,
            **options,
        )

    async def _resolve(self) -> None:
        if self._dsn is None or self.outbox.datasource is not None:
            return
        from pyfly.core.config import Config
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        datasource = _context_datasource(self._dsn)
        if datasource is None:
            registry = DataSourceRegistry(Config({"pyfly": {"data": {"relational": {"url": self._dsn}}}}))
            self._own_registry = registry
            datasource = registry.primary
        self.outbox.use_datasource(datasource)

    def _owns_datasource(self) -> bool:
        return self._dsn is not None and (self._own_registry is not None or self.outbox.datasource is None)

    async def _release_datasource(self) -> None:
        registry, self._own_registry = self._own_registry, None
        if registry is not None:
            self.outbox.use_datasource(None)
            await registry.close()


def _context_datasource(url: str) -> Any:
    """The datasource with *url* in the running application context's registry, or ``None``."""
    from pyfly.data.transaction import installed_registry

    managers = installed_registry()
    if managers is None:
        return None
    for name in managers.names():
        data_source = getattr(managers.get(name), "data_source", None)
        registry = getattr(data_source, "registry", None)
        if registry is not None:
            found = registry.find_by_url(url)
            if found is not None:
                return found
    return None
