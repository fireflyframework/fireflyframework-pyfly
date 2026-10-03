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
"""MemoryFlagStore (sources.store.driver=memory)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from pyfly.feature_flags.store.memory import MemoryFlagStore
from tests.feature_flags.store_contract import FlagStoreContract
from tests.feature_flags.support import bool_flag


class TestMemoryFlagStore(FlagStoreContract):
    @pytest.fixture
    async def store(self) -> AsyncIterator[MemoryFlagStore]:
        store = MemoryFlagStore()
        await store.start()
        yield store
        await store.stop()


async def test_failed_delete_keeps_row_and_audit_history() -> None:
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("clock unavailable")
        return datetime(2026, 10, 3, tzinfo=UTC)

    store = MemoryFlagStore(clock=clock)
    first = await store.put("a", bool_flag(), actor="ana")
    with pytest.raises(RuntimeError, match="clock unavailable"):
        await store.delete("a", actor="ops")
    stored = await store.get("a")
    assert stored is not None and stored.version == 1 and stored.definition == bool_flag()
    assert await store.revision() == first.id
    assert await store.history("a") == [first]
