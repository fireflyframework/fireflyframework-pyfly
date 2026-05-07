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
"""Signal delivery between an external caller and a waiting workflow."""

from __future__ import annotations

import asyncio
from typing import Any

from pyfly.transactional.core.context import ExecutionContext


class SignalService:
    """Routes named signals to specific workflow executions.

    The active :class:`ExecutionContext` is registered by correlation_id at
    workflow start; external callers hand a payload to :meth:`deliver` and the
    matching context's :meth:`ExecutionContext.deliver_signal` resumes the
    waiting step.
    """

    def __init__(self) -> None:
        self._contexts: dict[str, ExecutionContext] = {}
        self._lock = asyncio.Lock()

    async def register(self, ctx: ExecutionContext) -> None:
        async with self._lock:
            self._contexts[ctx.correlation_id] = ctx

    async def unregister(self, correlation_id: str) -> None:
        async with self._lock:
            self._contexts.pop(correlation_id, None)

    async def deliver(self, correlation_id: str, signal: str, payload: Any = None) -> bool:
        async with self._lock:
            ctx = self._contexts.get(correlation_id)
        if ctx is None:
            return False
        return await ctx.deliver_signal(signal, payload)

    async def list_active(self) -> list[str]:
        async with self._lock:
            return list(self._contexts.keys())
