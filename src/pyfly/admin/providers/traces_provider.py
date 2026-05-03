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
"""Traces data provider -- wraps TraceCollectorFilter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.admin.middleware.trace_collector import TraceCollectorFilter


class TracesProvider:
    """Provides HTTP trace data."""

    def __init__(self, collector: TraceCollectorFilter | None = None) -> None:
        self._collector = collector

    async def get_traces(self, limit: int = 100) -> dict[str, Any]:
        if self._collector is None:
            return {"traces": [], "total": 0}
        traces = self._collector.get_traces()
        recent = list(reversed(traces))[:limit]
        return {"traces": recent, "total": len(traces)}
