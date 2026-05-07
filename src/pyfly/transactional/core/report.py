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
"""Execution reports — final per-run summaries surfaced to API callers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pyfly.transactional.core.context import ExecutionContext, StepRecord
from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus, StepStatus


@dataclass
class StepReport:
    step_id: str
    status: StepStatus
    attempts: int
    started_at: datetime | None
    completed_at: datetime | None
    latency_ms: float
    result: Any
    error: str | None


@dataclass
class CompensationReport:
    step_id: str
    status: StepStatus
    result: Any
    error: str | None


@dataclass
class ExecutionReport:
    name: str
    pattern: ExecutionPattern
    correlation_id: str
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float
    error: str | None
    steps: list[StepReport] = field(default_factory=list)
    compensations: list[CompensationReport] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)


class ExecutionReportBuilder:
    """Produce an :class:`ExecutionReport` from an :class:`ExecutionContext`."""

    @staticmethod
    def build(ctx: ExecutionContext) -> ExecutionReport:
        steps: list[StepReport] = []
        compensations: list[CompensationReport] = []
        for sid, rec in ctx.get_all_steps().items():
            steps.append(_step_report(sid, rec))
            if rec.compensation_result is not None or rec.compensation_error is not None:
                compensations.append(
                    CompensationReport(
                        step_id=sid,
                        status=rec.status,
                        result=rec.compensation_result,
                        error=rec.compensation_error,
                    )
                )
        duration_ms = 0.0
        if ctx.completed_at is not None:
            duration_ms = (ctx.completed_at - ctx.started_at).total_seconds() * 1000.0
        return ExecutionReport(
            name=ctx.name,
            pattern=ctx.pattern,
            correlation_id=ctx.correlation_id,
            status=ctx.status,
            started_at=ctx.started_at,
            completed_at=ctx.completed_at,
            duration_ms=duration_ms,
            error=ctx.error,
            steps=steps,
            compensations=compensations,
            variables=ctx.get_all_variables(),
        )


def _step_report(step_id: str, rec: StepRecord) -> StepReport:
    return StepReport(
        step_id=step_id,
        status=rec.status,
        attempts=rec.attempts,
        started_at=rec.started_at,
        completed_at=rec.completed_at,
        latency_ms=rec.latency_ms,
        result=rec.result,
        error=rec.error,
    )
