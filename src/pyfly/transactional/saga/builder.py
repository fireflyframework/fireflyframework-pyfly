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
"""Programmatic saga builder — alternative to the ``@saga`` decorator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.step_definition import StepDefinition


class SagaBuilder:
    """Build a :class:`SagaDefinition` without decorators."""

    def __init__(self, name: str, *, layer_concurrency: int = 0) -> None:
        self._definition = SagaDefinition(name=name, bean=None, layer_concurrency=layer_concurrency)

    def step(
        self,
        step_id: str,
        handler: Callable[..., Awaitable[Any] | Any],
        *,
        depends_on: list[str] | None = None,
        retry: int = 0,
        backoff_ms: int = 0,
        timeout_ms: int = 0,
        jitter: bool = False,
        jitter_factor: float = 0.0,
        cpu_bound: bool = False,
        idempotency_key: str | None = None,
        compensation: Callable[..., Any] | None = None,
        compensation_critical: bool = False,
    ) -> SagaBuilder:
        step = StepDefinition(
            id=step_id,
            step_method=handler,
            compensate_name=getattr(compensation, "__name__", None) if compensation else None,
            compensate_method=compensation,
            depends_on=list(depends_on or []),
            retry=retry,
            backoff_ms=backoff_ms,
            timeout_ms=timeout_ms,
            jitter=jitter,
            jitter_factor=jitter_factor,
            cpu_bound=cpu_bound,
            idempotency_key=idempotency_key,
            compensation_critical=compensation_critical,
        )
        self._definition.steps[step_id] = step
        return self

    def build(self) -> SagaDefinition:
        return self._definition
