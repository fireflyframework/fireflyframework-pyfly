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
"""The harness of the repository contract suites on the backend matrix (WP03).

``repository_datasources(backend, *models)`` builds the application's :class:`DataSourceRegistry` from the
lane's configuration (so SQLite gets the framework's own settings: foreign keys on, WAL, the BEGIN recipe),
creates the tables of *models*, installs the registry's transaction managers (managed repositories and
``@transactional`` resolve them), and on exit checks that no pooled connection is left checked out before it
closes the registry. :func:`dml` reduces a :class:`StatementCounter` to the statements that touch rows, so a
count reads the same on every lane (SQLite's explicit ``BEGIN`` and MySQL's ``SET TRANSACTION READ ONLY`` are
transaction control, not work).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.transaction_manager import transaction_managers_for
from pyfly.data.transaction import install_registry, uninstall_registry
from pyfly.testing import StatementCounter
from tests.support.backend_matrix import RelationalBackend

ROW_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")


class Datasources:
    """The registry of one test, and helpers over its primary engine."""

    def __init__(self, registry: DataSourceRegistry) -> None:
        self.registry = registry

    @property
    def engine(self) -> AsyncEngine:
        return self.registry.engine()

    @property
    def dialect(self) -> str:
        return self.registry.primary.capabilities.dialect

    def counter(self) -> StatementCounter:
        """A statement counter on the primary engine (use it as a context manager)."""
        return StatementCounter(self.engine)

    def checked_out(self) -> int:
        return sum(datasource.engine.sync_engine.pool.checkedout() for datasource in self.registry.all_datasources())


def dml(counter: StatementCounter) -> dict[str, int]:
    """The counter's statements that read or write rows, by verb."""
    return {verb: count for verb, count in counter.counts().items() if verb in ROW_VERBS}


def sql_of(counter: StatementCounter, verb: str) -> list[str]:
    """The SQL text of every recorded statement with *verb*, in order."""
    return [statement.sql for statement in counter.statements if statement.verb == verb.upper()]


@contextlib.asynccontextmanager
async def repository_datasources(
    backend: RelationalBackend, *models: Any, overrides: dict[str, Any] | None = None
) -> AsyncIterator[Datasources]:
    """The application's datasources on *backend*, with the tables of *models* and the managers installed."""
    await backend.create_tables(*models)
    registry = DataSourceRegistry(backend.config(overrides))
    managers = transaction_managers_for(registry)
    install_registry(managers)
    datasources = Datasources(registry)
    try:
        yield datasources
        assert datasources.checked_out() == 0, "a repository call left a pooled connection checked out"
    finally:
        uninstall_registry(managers)
        await registry.close()
