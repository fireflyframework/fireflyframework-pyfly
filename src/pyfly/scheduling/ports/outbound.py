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
"""Task executor port — abstract interface for executing async tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class TaskExecutorPort(Protocol):
    """Port for executing async tasks."""

    async def submit(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Submit a coroutine for execution. Returns an asyncio.Task."""
        ...

    async def start(self) -> None:
        """Initialize the executor."""
        ...

    async def stop(self) -> None:
        """Stop the executor, releasing resources."""
        ...
