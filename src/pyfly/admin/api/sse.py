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
"""Server-Sent Events for real-time admin dashboard updates."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from pyfly.admin.log_handler import AdminLogHandler
    from pyfly.admin.middleware.trace_collector import TraceCollectorFilter
    from pyfly.admin.providers.beans_provider import BeansProvider
    from pyfly.admin.providers.health_provider import HealthProvider
    from pyfly.admin.providers.metrics_provider import MetricsProvider
    from pyfly.admin.providers.runtime_provider import RuntimeProvider
    from pyfly.admin.providers.server_provider import ServerProvider

_logger = logging.getLogger(__name__)


def _sse_event(data: Any, event: str | None = None) -> str:
    payload = json.dumps(data)
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {payload}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


async def health_stream(
    health_provider: HealthProvider,
    interval: float = 5.0,
) -> AsyncGenerator[str, None]:
    last_status = None
    try:
        while True:
            data = await health_provider.get_health()
            if data.get("status") != last_status:
                yield _sse_event(data, event="health")
                last_status = data.get("status")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _logger.debug("sse_stream_closed", extra={"stream": "health"})


async def metrics_stream(
    metrics_provider: MetricsProvider,
    interval: float = 5.0,
) -> AsyncGenerator[str, None]:
    try:
        while True:
            # Stream live values (not just names) so the admin trend is push-based.
            data = await metrics_provider.get_metric_values()
            yield _sse_event(data, event="metrics")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _logger.debug("sse_stream_closed", extra={"stream": "metrics"})


async def traces_stream(
    collector: TraceCollectorFilter | None,
    interval: float = 2.0,
) -> AsyncGenerator[str, None]:
    last_count = 0
    try:
        while True:
            if collector is not None:
                traces = collector.get_traces()
                current_count = len(traces)
                if current_count > last_count:
                    new_traces = list(traces)[last_count:]
                    for trace in new_traces:
                        yield _sse_event(trace, event="trace")
                    last_count = current_count
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _logger.debug("sse_stream_closed", extra={"stream": "traces"})


async def logfile_stream(
    log_handler: AdminLogHandler | None,
    interval: float = 1.0,
) -> AsyncGenerator[str, None]:
    last_id = 0
    try:
        while True:
            if log_handler is not None:
                records = log_handler.get_records(after=last_id)
                for record in records:
                    yield _sse_event(record, event="log")
                    last_id = record["id"]
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _logger.debug("sse_stream_closed", extra={"stream": "logfile"})


async def runtime_stream(
    runtime_provider: RuntimeProvider,
    interval: float = 5.0,
) -> AsyncGenerator[str, None]:
    try:
        while True:
            data = await runtime_provider.get_runtime()
            yield _sse_event(data, event="runtime")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _logger.debug("sse_stream_closed", extra={"stream": "runtime"})


async def server_stream(
    server_provider: ServerProvider,
    interval: float = 5.0,
) -> AsyncGenerator[str, None]:
    try:
        while True:
            data = await server_provider.get_server_info()
            yield _sse_event(data, event="server")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _logger.debug("sse_stream_closed", extra={"stream": "server"})


async def beans_stream(
    beans_provider: BeansProvider,
    interval: float = 10.0,
) -> AsyncGenerator[str, None]:
    last_counts: dict[str, int] = {}
    try:
        while True:
            data = await beans_provider.get_beans()
            updates = []
            for bean in data.get("beans", []):
                name = bean["name"]
                count = bean.get("resolution_count", 0)
                if count != last_counts.get(name, 0):
                    updates.append({"name": name, "resolution_count": count})
                    last_counts[name] = count
            if updates:
                yield _sse_event({"updates": updates}, event="beans")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _logger.debug("sse_stream_closed", extra={"stream": "beans"})


def make_sse_response(generator: AsyncGenerator[str, None]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
