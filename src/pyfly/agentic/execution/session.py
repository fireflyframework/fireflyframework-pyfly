# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public synchronous persistent execution contract."""

from collections.abc import Mapping
from types import TracebackType
from typing import Protocol, Self

from pyfly.agentic.execution.session_types import (
    ExecutionFeedResult,
    ExecutionSessionState,
    ExecutionSessionUsage,
    ExecutionValue,
)
from pyfly.agentic.execution.types import Capability


class ExecutionSession(Protocol):
    """One thread-owned, non-reentrant interpreter; no implicit replay or reset."""

    @property
    def state(self) -> ExecutionSessionState: ...
    @property
    def capabilities(self) -> frozenset[Capability]: ...
    @property
    def usage(self) -> ExecutionSessionUsage: ...
    def feed(self, code: str, *, inputs: Mapping[str, ExecutionValue] | None = None) -> ExecutionFeedResult: ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None: ...
