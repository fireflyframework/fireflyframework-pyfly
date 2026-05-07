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
"""Exception hierarchy for the orchestration engine."""

from __future__ import annotations


class OrchestrationError(Exception):
    """Base class for every error raised by the orchestration engine."""


class OrchestrationValidationError(OrchestrationError):
    """A saga / workflow / TCC definition failed structural validation."""


class StepFailedError(OrchestrationError):
    """Raised when a step exhausts its retries.

    Wraps the underlying exception so callers can distinguish *application*
    failures from engine errors.
    """

    def __init__(self, step_id: str, attempts: int, cause: BaseException) -> None:
        super().__init__(f"step '{step_id}' failed after {attempts} attempt(s): {cause}")
        self.step_id = step_id
        self.attempts = attempts
        self.cause = cause


class StepTimeoutError(OrchestrationError):
    """Raised when a step exceeds its configured timeout."""

    def __init__(self, step_id: str, timeout_ms: int) -> None:
        super().__init__(f"step '{step_id}' timed out after {timeout_ms}ms")
        self.step_id = step_id
        self.timeout_ms = timeout_ms


class ExecutionNotFoundError(OrchestrationError):
    """The requested execution could not be found in the persistence store."""


class SignalDeliveryError(OrchestrationError):
    """A workflow signal could not be delivered."""


class CompensationFailedError(OrchestrationError):
    """A saga compensation action raised."""

    def __init__(self, step_id: str, cause: BaseException) -> None:
        super().__init__(f"compensation for step '{step_id}' failed: {cause}")
        self.step_id = step_id
        self.cause = cause


class WorkflowSuspendedError(OrchestrationError):
    """Internal sentinel raised when a workflow step pauses on a signal/timer.

    This is *not* a user-visible error — the workflow executor catches it,
    persists state, and returns control to the caller.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
