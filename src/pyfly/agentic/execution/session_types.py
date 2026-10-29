# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public data and host-only control types for persistent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pyfly.agentic.execution.types import ExecutionLimits, ExecutionResult

type ExecutionValue = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | list[ExecutionValue]
    | tuple[ExecutionValue, ...]
    | dict[str, ExecutionValue]
)


@dataclass(frozen=True, slots=True)
class ExecutionSessionLimits:
    """Fixed session ceilings; trusted sync callbacks own their I/O deadlines.

    ``execution.timeout_seconds`` bounds worker turns and VM compute, not an
    entire feed including callbacks. Session lifetime is checked cooperatively.
    Unset execution fields use session defaults, independently of environment
    defaults. Configuration is copied before a worker is opened.
    """

    execution: ExecutionLimits = field(
        default_factory=lambda: ExecutionLimits(
            timeout_seconds=30.0,
            max_memory_bytes=64 * 1024 * 1024,
            max_recursion_depth=200,
        )
    )
    max_session_seconds: float = 300.0
    max_feeds: int = 32
    max_external_calls: int = 256
    max_suspensions: int = 4096
    max_output_bytes: int = 1024 * 1024
    max_value_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecutionHalt:
    """Host callback control: accept a value and stop without resuming guest code."""

    value: ExecutionValue = None


@dataclass(frozen=True, slots=True)
class ExecutionReject:
    """Host callback control: reject this feed and drain without more host calls."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ExecutionFeedback:
    """Trusted rejection metadata, independent of untrusted guest output."""

    code: str
    message: str
    function_name: str
    call_index: int


@dataclass(frozen=True, slots=True)
class ExecutionSessionUsage:
    """Cumulative counters; no feed resets the session's budgets."""

    feeds: int
    external_calls: int
    suspensions: int
    output_bytes: int
    elapsed_seconds: float


class ExecutionSessionState(StrEnum):
    """Lifecycle state; only READY admits another feed."""

    READY = "ready"
    RUNNING = "running"
    HALTED = "halted"
    CLOSED = "closed"
    BROKEN = "broken"


class ExecutionFeedStatus(StrEnum):
    """Feed/control outcome, separate from the session cleanup state."""

    COMPLETED = "completed"
    ERROR = "error"
    REJECTED = "rejected"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class ExecutionFeedResult:
    """Feed result, trusted feedback and cumulative usage.

    An accepted HALTED control with failed cleanup has state BROKEN and
    ``result.success=False``; its accepted value remains in ``result.output``.
    It must never be replayed. All guest stdout/stderr remains untrusted.
    """

    status: ExecutionFeedStatus
    result: ExecutionResult
    state: ExecutionSessionState
    feedback: tuple[ExecutionFeedback, ...]
    usage: ExecutionSessionUsage


class ExecutionSessionError(RuntimeError):
    """An operation violated session ownership or lifecycle constraints."""
