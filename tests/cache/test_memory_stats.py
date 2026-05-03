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
"""Tests for InMemoryCache introspection methods (get_stats, get_keys)."""

from datetime import timedelta
from unittest.mock import patch

from pyfly.cache.adapters.memory import InMemoryCache


class TestInMemoryCacheStats:
    async def test_get_stats_empty(self):
        cache = InMemoryCache()
        stats = cache.get_stats()
        assert stats["size"] == 0
        assert stats["type"] == "memory"
        assert stats["max_size"] is None

    async def test_get_stats_with_entries(self):
        cache = InMemoryCache()
        await cache.put("a", 1)
        await cache.put("b", 2)
        await cache.put("c", 3)
        stats = cache.get_stats()
        assert stats["size"] == 3

    async def test_get_stats_expired_excluded(self):
        cache = InMemoryCache()
        await cache.put("alive", "yes")
        await cache.put("dead", "no", ttl=timedelta(seconds=1))

        # Simulate time passing so that "dead" expires
        with patch("pyfly.cache.adapters.memory.time") as mock_time:
            # First call for put was real; now advance monotonic clock
            mock_time.monotonic.return_value = 1e12  # far in the future
            stats = cache.get_stats()
            assert stats["size"] == 1  # only "alive"

    async def test_get_keys(self):
        cache = InMemoryCache()
        await cache.put("user:1", {"name": "Alice"})
        await cache.put("user:2", {"name": "Bob"})
        await cache.put("session:abc", "token")
        keys = cache.get_keys()
        assert sorted(keys) == ["session:abc", "user:1", "user:2"]

    async def test_get_keys_expired_excluded(self):
        cache = InMemoryCache()
        await cache.put("fresh", "yes")
        await cache.put("stale", "no", ttl=timedelta(seconds=1))

        with patch("pyfly.cache.adapters.memory.time") as mock_time:
            mock_time.monotonic.return_value = 1e12
            keys = cache.get_keys()
            assert keys == ["fresh"]
