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
"""Regression tests for #189 — bulkhead sync/async share one permit counter."""

from __future__ import annotations

import pytest

from pyfly.kernel.exceptions import BulkheadException
from pyfly.resilience.bulkhead import Bulkhead, bulkhead


class TestBulkheadUnifiedAccounting:
    def test_no_separate_semaphore_to_diverge(self):
        # The divergent asyncio.Semaphore is gone — a single counter is the
        # source of truth for both sync and async paths (audit #189).
        bh = Bulkhead(max_concurrent=2)
        assert not hasattr(bh, "_semaphore")

    def test_sync_wrapper_consumes_and_releases_shared_permit(self):
        bh = Bulkhead(max_concurrent=1)
        observed: dict[str, int] = {}

        @bulkhead(bh)
        def fn() -> str:
            observed["active"] = bh._active
            observed["slots"] = bh.available_slots
            return "ok"

        assert fn() == "ok"
        assert observed["active"] == 1  # a permit was held during the call
        assert observed["slots"] == 0
        assert bh.available_slots == 1  # released afterwards

    async def test_sync_blocked_while_async_holds_the_slot(self):
        bh = Bulkhead(max_concurrent=1)
        await bh.acquire()  # async side holds the only permit

        @bulkhead(bh)
        def fn() -> str:
            return "ok"

        with pytest.raises(BulkheadException):
            fn()  # sync path sees the same exhausted counter

        bh.release()
        assert fn() == "ok"

    async def test_mixed_sync_async_returns_to_full_capacity(self):
        bh = Bulkhead(max_concurrent=2)

        @bulkhead(bh)
        def sync_fn() -> str:
            return "s"

        @bulkhead(bh)
        async def async_fn() -> str:
            return "a"

        assert sync_fn() == "s"
        assert await async_fn() == "a"
        assert sync_fn() == "s"
        assert bh.available_slots == 2  # no leak, no divergence
