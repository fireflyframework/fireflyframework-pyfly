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
"""Tests for RuntimeProvider."""

import time

from pyfly.admin.providers.runtime_provider import RuntimeProvider


class TestRuntimeProvider:
    async def test_returns_memory_info(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        assert "memory" in data
        assert "rss_mb" in data["memory"]
        assert data["memory"]["rss_mb"] > 0

    async def test_returns_thread_count(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        assert "threads" in data
        assert data["threads"]["active"] >= 1

    async def test_returns_gc_stats(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        assert "gc" in data
        assert "collections" in data["gc"]
        assert len(data["gc"]["collections"]) == 3  # gen0, gen1, gen2

    async def test_returns_cpu_time(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        assert "cpu" in data
        assert "process_time_s" in data["cpu"]
        assert data["cpu"]["process_time_s"] >= 0

    async def test_returns_timestamp(self):
        before = time.time()
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        after = time.time()
        assert "timestamp" in data
        assert before <= data["timestamp"] <= after

    async def test_returns_python_info(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        assert "python" in data
        assert "version" in data["python"]
        assert "implementation" in data["python"]

    async def test_thread_names_includes_main(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        names = data["threads"]["names"]
        assert any("Main" in n or "main" in n.lower() for n in names)

    async def test_gc_enabled(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        assert data["gc"]["enabled"] is True

    async def test_gc_thresholds(self):
        provider = RuntimeProvider()
        data = await provider.get_runtime()
        assert len(data["gc"]["thresholds"]) == 3
