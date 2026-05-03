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
"""PyFly Saga — orchestration-based distributed transactions."""

from __future__ import annotations

from pyfly.transactional.saga.annotations import (
    CompensationError,
    FromCompensationResult,
    FromStep,
    Header,
    Headers,
    Input,
    SetVariable,
    Variable,
    Variables,
    compensation_step,
    external_step,
    saga,
    saga_step,
    step_event,
)
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.core.report import FailureReport
from pyfly.transactional.saga.core.result import SagaResult, StepOutcome
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.registry.saga_builder import SagaBuilder

__all__ = [
    "CompensationError",
    "FailureReport",
    "FromCompensationResult",
    "FromStep",
    "Header",
    "Headers",
    "Input",
    "SagaBuilder",
    "SagaContext",
    "SagaEngine",
    "SagaResult",
    "SetVariable",
    "StepOutcome",
    "Variable",
    "Variables",
    "compensation_step",
    "external_step",
    "saga",
    "saga_step",
    "step_event",
]
