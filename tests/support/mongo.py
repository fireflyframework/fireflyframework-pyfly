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
"""Helpers for the MongoDB tests: a Beanie database of the test's own, and the commands a client sent.

:func:`beanie_database` binds document classes to a fresh database on a real server (the replica set of
``mongo_rs_url``, or a standalone one), with a :class:`CommandLog` listening to the client, so a test can
count the round trips an operation costs and read what each command carried (its session, projection or
filter).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from beanie import init_beanie
from pymongo import AsyncMongoClient, monitoring

DATA_COMMANDS = frozenset(
    {
        "find",
        "insert",
        "update",
        "delete",
        "aggregate",
        "count",
        "getMore",
        "findAndModify",
        "distinct",
        "commitTransaction",
        "abortTransaction",
    }
)
"""The commands that read or write documents (connection handshakes and session bookkeeping left out)."""


@dataclass
class CommandLog(monitoring.CommandListener):
    """Every command a client started: its name and its body."""

    commands: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if event.command_name in DATA_COMMANDS:
            self.commands.append((event.command_name, dict(event.command)))

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        pass

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        pass

    def clear(self) -> None:
        self.commands.clear()

    def names(self) -> list[str]:
        """The names of the commands, in order."""
        return [name for name, _body in self.commands]


@dataclass
class BeanieDatabase:
    """A database of the test's own, with the document classes bound to it."""

    client: AsyncMongoClient[Any]
    name: str
    log: CommandLog

    @property
    def database(self) -> Any:
        return self.client[self.name]


@contextlib.asynccontextmanager
async def beanie_database(url: str, models: Sequence[type], **client_options: Any) -> AsyncIterator[BeanieDatabase]:
    """Bind *models* to a new database on the server at *url*, and drop it afterwards."""
    log = CommandLog()
    client: AsyncMongoClient[Any] = AsyncMongoClient(url, event_listeners=[log], **client_options)
    name = f"pyfly_t_{uuid.uuid4().hex[:12]}"
    try:
        await init_beanie(database=client[name], document_models=list(models))
        log.clear()
        yield BeanieDatabase(client, name, log)
    finally:
        with contextlib.suppress(Exception):
            await client.drop_database(name)
        await client.close()
