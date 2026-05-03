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
"""Saga definition — aggregate root that groups step definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.transactional.saga.registry.step_definition import StepDefinition


@dataclass
class SagaDefinition:
    """Mutable aggregate holding the complete definition of a registered saga.

    Built incrementally by :class:`SagaRegistry` during bean scanning.

    Attributes:
        name: Unique saga name (from ``@saga(name=...)``).
        bean: The saga bean instance whose methods implement the steps.
        layer_concurrency: Maximum number of steps executed concurrently
            within a single dependency layer.  ``0`` means unlimited.
        steps: Mapping of step id to its :class:`StepDefinition`.
    """

    name: str
    bean: Any
    layer_concurrency: int = 0
    steps: dict[str, StepDefinition] = field(default_factory=dict)
