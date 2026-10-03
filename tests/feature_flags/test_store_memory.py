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

import pytest

from pyfly.feature_flags.store.memory import MemoryFlagStore
from tests.feature_flags.store_contract import FlagStoreContract


class TestMemoryFlagStore(FlagStoreContract):
    @pytest.fixture
    async def store(self) -> AsyncIterator[MemoryFlagStore]:
        store = MemoryFlagStore()
        await store.start()
        yield store
        await store.stop()
