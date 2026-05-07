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
"""Lightweight metrics collector for orchestration engine internals.

Adapter-friendly: stores counters / histograms in memory; the optional
``prometheus_client`` integration in :mod:`pyfly.observability` can wrap this
to publish standard Prometheus metrics.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

from pyfly.transactional.core.events import _BaseOrchestrationEvents
from pyfly.transactional.core.model import ExecutionPattern, TccPhase


@dataclass
class _Histogram:
    count: int = 0
    sum: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    samples: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.count += 1
        self.sum += value
        self.samples.append(value)
        # Keep last 1000 samples
        if len(self.samples) > 1000:
            self.samples = self.samples[-1000:]
        ordered = sorted(self.samples)
        n = len(ordered)
        self.p50 = ordered[int(n * 0.5)] if n > 0 else 0.0
        self.p95 = ordered[int(n * 0.95)] if n > 0 else 0.0


class OrchestrationMetrics(_BaseOrchestrationEvents):
    """In-memory metrics view of orchestration activity.

    Plug it into a :class:`CompositeOrchestrationEvents` to capture metrics
    transparently.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.executions_started: dict[str, int] = defaultdict(int)
        self.executions_completed: dict[str, int] = defaultdict(int)
        self.executions_failed: dict[str, int] = defaultdict(int)
        self.execution_duration: dict[str, _Histogram] = defaultdict(_Histogram)
        self.steps_started: dict[str, int] = defaultdict(int)
        self.steps_succeeded: dict[str, int] = defaultdict(int)
        self.steps_failed: dict[str, int] = defaultdict(int)
        self.step_latency: dict[str, _Histogram] = defaultdict(_Histogram)
        self.compensations: dict[str, int] = defaultdict(int)
        self.compensation_failures: dict[str, int] = defaultdict(int)
        self.dead_letters: int = 0
        self.tcc_phases: dict[TccPhase, int] = defaultdict(int)

    async def on_start(self, *, name: str, pattern: ExecutionPattern, correlation_id: str) -> None:
        async with self._lock:
            self.executions_started[name] += 1

    async def on_completed(
        self, *, name: str, pattern: ExecutionPattern, correlation_id: str, success: bool, duration_ms: float
    ) -> None:
        async with self._lock:
            if success:
                self.executions_completed[name] += 1
            else:
                self.executions_failed[name] += 1
            self.execution_duration[name].add(duration_ms)

    async def on_step_started(self, *, name: str, correlation_id: str, step_id: str) -> None:
        async with self._lock:
            self.steps_started[f"{name}.{step_id}"] += 1

    async def on_step_success(
        self, *, name: str, correlation_id: str, step_id: str, attempts: int, latency_ms: float
    ) -> None:
        async with self._lock:
            self.steps_succeeded[f"{name}.{step_id}"] += 1
            self.step_latency[f"{name}.{step_id}"].add(latency_ms)

    async def on_step_failed(
        self, *, name: str, correlation_id: str, step_id: str, error: BaseException, attempts: int, latency_ms: float
    ) -> None:
        async with self._lock:
            self.steps_failed[f"{name}.{step_id}"] += 1
            self.step_latency[f"{name}.{step_id}"].add(latency_ms)

    async def on_step_compensated(
        self, *, name: str, correlation_id: str, step_id: str, error: BaseException | None
    ) -> None:
        async with self._lock:
            self.compensations[f"{name}.{step_id}"] += 1
            if error is not None:
                self.compensation_failures[f"{name}.{step_id}"] += 1

    async def on_phase_started(self, *, name: str, correlation_id: str, phase: TccPhase) -> None:
        async with self._lock:
            self.tcc_phases[phase] += 1

    async def on_dead_lettered(
        self, *, name: str, correlation_id: str, step_id: str | None, error: BaseException
    ) -> None:
        async with self._lock:
            self.dead_letters += 1

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-friendly snapshot for ``/actuator/metrics`` endpoints."""
        return {
            "executions": {
                name: {
                    "started": self.executions_started.get(name, 0),
                    "completed": self.executions_completed.get(name, 0),
                    "failed": self.executions_failed.get(name, 0),
                    "duration_p50_ms": self.execution_duration.get(name, _Histogram()).p50,
                    "duration_p95_ms": self.execution_duration.get(name, _Histogram()).p95,
                }
                for name in set(self.executions_started) | set(self.executions_completed) | set(self.executions_failed)
            },
            "steps": {
                key: {
                    "started": self.steps_started.get(key, 0),
                    "succeeded": self.steps_succeeded.get(key, 0),
                    "failed": self.steps_failed.get(key, 0),
                    "p50_ms": self.step_latency.get(key, _Histogram()).p50,
                    "p95_ms": self.step_latency.get(key, _Histogram()).p95,
                }
                for key in set(self.steps_started) | set(self.steps_succeeded) | set(self.steps_failed)
            },
            "compensations": dict(self.compensations),
            "compensation_failures": dict(self.compensation_failures),
            "dead_letters": self.dead_letters,
            "tcc_phases": {p.value: c for p, c in self.tcc_phases.items()},
        }
