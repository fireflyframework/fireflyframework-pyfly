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
"""Mutable context shared across sagas during composition execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.transactional.saga.core.result import SagaResult


@dataclass
class CompositionContext:
    """Mutable state that accompanies a composition execution.

    The :class:`SagaCompositor` populates this context as sagas
    complete, and the :class:`DataFlowManager` reads from it to resolve
    inputs for downstream sagas.

    Attributes
    ----------
    correlation_id:
        Unique identifier for this composition execution.
    composition_name:
        Name of the :class:`SagaComposition` being executed.
    saga_results:
        Mapping of saga name to its :class:`SagaResult` upon completion.
    saga_inputs:
        Mapping of saga name to the resolved input passed to it.
    compensated_sagas:
        Names of sagas that were compensated after a failure.
    error:
        The exception that caused the composition to fail, or ``None``.
    """

    correlation_id: str
    composition_name: str
    saga_results: dict[str, SagaResult] = field(default_factory=dict)
    saga_inputs: dict[str, Any] = field(default_factory=dict)
    compensated_sagas: list[str] = field(default_factory=list)
    error: Exception | None = None
