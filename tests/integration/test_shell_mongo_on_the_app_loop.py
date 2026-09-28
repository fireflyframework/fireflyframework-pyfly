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
"""A shell command uses the application's Mongo client (WP13-09, C077).

pymongo's ``AsyncMongoClient`` is bound to the loop it was first used on (``init_beanie`` uses it at
startup), and shell commands used to run on a private loop: every command that touched Mongo failed with
"Cannot use AsyncMongoClient in different event loop". Here a client is used at startup on the application's
loop, as the document module does, and a one-shot command and a REPL command then use it, on the replica set.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from pyfly.container.stereotypes import component, shell_component
from pyfly.container.types import Scope
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.shell.adapters.click_adapter import ClickShellAdapter
from pyfly.shell.decorators import shell_method
from pyfly.shell.ports.outbound import ShellRunnerPort
from tests.support.backend_matrix import new_database_name


class MongoSettings:
    url = ""
    database = ""


@component
class Documents:
    """Owns a client it uses at startup, on the application's loop (as ``init_beanie`` does)."""

    def __init__(self) -> None:
        self.client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(MongoSettings.url)

    async def start(self) -> None:
        await self.client.admin.command("ping")

    async def stop(self) -> None:
        await self.client.close()


@shell_component
class DocumentCommands:
    def __init__(self, documents: Documents) -> None:
        self.documents = documents

    @shell_method(key="store", help="Store a document")
    async def store(self, name: str) -> str:
        await self.documents.client[MongoSettings.database].items.insert_one({"name": name})
        return f"stored {name}"

    @shell_method(key="count", help="Count the documents")
    async def count(self) -> str:
        return f"count={await self.documents.client[MongoSettings.database].items.count_documents({})}"


@pytest.fixture
async def shell(mongo_rs_url: str) -> AsyncIterator[ClickShellAdapter]:
    MongoSettings.url = mongo_rs_url
    MongoSettings.database = new_database_name()
    runner = ClickShellAdapter()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Documents)
    ctx.register_bean(DocumentCommands)
    ctx._container.register(ShellRunnerPort, scope=Scope.SINGLETON)
    ctx._container._registrations[ShellRunnerPort].instance = runner
    await ctx.start()
    documents = ctx.get_bean(Documents)
    try:
        yield runner
    finally:
        await documents.client.drop_database(MongoSettings.database)
        await ctx.stop()


async def test_commands_use_the_client_the_application_started(
    shell: ClickShellAdapter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await shell.run(["store", "a"]) == 0

    lines = ["store b", "count"]

    def typed(prompt: str = "") -> str:
        if not lines:
            raise EOFError
        return lines.pop(0)

    monkeypatch.setattr(builtins, "input", typed)
    await shell.run_interactive()

    out = capsys.readouterr().out
    assert "stored a" in out and "stored b" in out and "count=2" in out
