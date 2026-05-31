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
"""Wiring regressions for CqrsAutoConfiguration."""

from __future__ import annotations

from pyfly.cqrs.config.auto_configuration import CqrsAutoConfiguration


class _FakeCache:
    async def get(self, key): ...
    async def put(self, key, value, ttl=None): ...
    async def evict(self, key): ...


def test_query_cache_adapter_uses_injected_cache():
    cfg = CqrsAutoConfiguration()
    fake = _FakeCache()
    adapter = cfg.query_cache_adapter(cache=fake)
    assert adapter._cache is fake


def test_query_cache_adapter_no_cache_is_noop():
    cfg = CqrsAutoConfiguration()
    adapter = cfg.query_cache_adapter()
    assert adapter._cache is None
