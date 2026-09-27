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
"""The command bus runs its query-cache invalidation in a shielded task only when there is something to
invalidate: a command with no cache key and no event that tags a cached query costs no task."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cache_evict
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.decorators import command_handler, query_handler
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Command, Query


@dataclass(frozen=True)
class NoteAdded:
    pass


@dataclass(frozen=True)
class Untagged:
    pass


@dataclass
class Outcome:
    domain_events: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class AddNote(Command[Outcome]):
    event: Any = None
    cache_key: str | None = None

    def get_cache_key(self) -> str | None:
        return self.cache_key


@command_handler
class AddNoteHandler(CommandHandler[AddNote, Outcome]):
    async def do_handle(self, command: AddNote) -> Outcome:
        return Outcome(domain_events=[command.event] if command.event is not None else [])


@dataclass(frozen=True)
class ListNotes(Query[list[str]]):
    pass


@cache_evict(NoteAdded)
@query_handler(cacheable=True)
class ListNotesHandler(QueryHandler[ListNotes, list[str]]):
    async def do_handle(self, query: ListNotes) -> list[str]:
        return []


async def _count_tasks(send: Coroutine[Any, Any, Any]) -> int:
    """How many tasks are created while *send* runs."""
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    created = 0

    def factory(event_loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        nonlocal created
        created += 1
        return asyncio.Task(coro, loop=event_loop, **kwargs)

    loop.set_task_factory(factory)
    try:
        await send
    finally:
        loop.set_task_factory(previous)
    return created


@pytest.fixture
def bus() -> DefaultCommandBus:
    registry = HandlerRegistry()
    registry.register_command_handler(AddNoteHandler())
    registry.register_query_handler(ListNotesHandler())
    return DefaultCommandBus(registry=registry, query_cache=QueryCacheAdapter(InMemoryCache()))


async def test_a_command_that_invalidates_nothing_starts_no_task(bus: DefaultCommandBus) -> None:
    assert await _count_tasks(bus.send(AddNote())) == 0
    assert await _count_tasks(bus.send(AddNote(event=Untagged()))) == 0  # no cached query is tagged with it


async def test_a_command_that_invalidates_runs_it_shielded(bus: DefaultCommandBus) -> None:
    assert await _count_tasks(bus.send(AddNote(cache_key="ListNotes:all"))) >= 1
    assert await _count_tasks(bus.send(AddNote(event=NoteAdded()))) >= 1
