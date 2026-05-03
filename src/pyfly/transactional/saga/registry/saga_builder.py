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
"""Saga builder — fluent DSL for programmatic saga creation.

Provides :class:`SagaBuilder` and :class:`StepBuilder` for constructing
saga definitions without decorators.  This is useful for dynamic sagas,
sagas loaded from configuration, or when the decorator syntax is not
appropriate.

Example::

    saga_def = (
        SagaBuilder("order-saga")
        .step("validate").handler(validate_fn).add()
        .step("reserve").handler(reserve_fn).compensate(release_fn)
            .depends_on("validate").retry(3).backoff_ms(100).add()
        .layer_concurrency(5)
        .build()
    )
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.saga_registry import SagaValidationError
from pyfly.transactional.saga.registry.step_definition import StepDefinition


class StepBuilder:
    """Builder for individual step configuration.

    Accumulates step metadata via a fluent interface.  Call :meth:`add` to
    finalise the step and return the parent :class:`SagaBuilder` for
    continued chaining.
    """

    def __init__(self, step_id: str, parent: SagaBuilder) -> None:
        self._step_id = step_id
        self._parent = parent
        self._handler_fn: Callable[..., Any] | None = None
        self._compensate_fn: Callable[..., Any] | None = None
        self._depends_on: list[str] = []
        self._retry: int = 0
        self._backoff_ms: int = 0
        self._timeout_ms: int = 0
        self._jitter: bool = False
        self._jitter_factor: float = 0.0
        self._cpu_bound: bool = False

    # ── Fluent setters ────────────────────────────────────────

    def handler(self, func: Callable[..., Any]) -> StepBuilder:
        """Set the forward-action handler for this step."""
        self._handler_fn = func
        return self

    def compensate(self, func: Callable[..., Any]) -> StepBuilder:
        """Set the compensation handler for this step."""
        self._compensate_fn = func
        return self

    def depends_on(self, *step_ids: str) -> StepBuilder:
        """Declare dependency on one or more preceding steps."""
        self._depends_on.extend(step_ids)
        return self

    def retry(self, count: int) -> StepBuilder:
        """Set the maximum number of retry attempts."""
        self._retry = count
        return self

    def backoff_ms(self, ms: int) -> StepBuilder:
        """Set the base backoff duration in milliseconds between retries."""
        self._backoff_ms = ms
        return self

    def timeout_ms(self, ms: int) -> StepBuilder:
        """Set the execution timeout in milliseconds."""
        self._timeout_ms = ms
        return self

    def jitter(self, enabled: bool = True, factor: float = 0.5) -> StepBuilder:
        """Enable jitter on the backoff duration.

        Args:
            enabled: Whether jitter is active.
            factor: Fraction of the backoff used as the jitter range.
        """
        self._jitter = enabled
        self._jitter_factor = factor
        return self

    def cpu_bound(self, enabled: bool = True) -> StepBuilder:
        """Mark this step as CPU-bound for offloading to a thread/process pool."""
        self._cpu_bound = enabled
        return self

    # ── Finalisation ──────────────────────────────────────────

    def add(self) -> SagaBuilder:
        """Finalise this step and return the parent builder for chaining."""
        self._parent._add_step(self)  # noqa: SLF001
        return self._parent

    def _build_definition(self) -> StepDefinition:
        """Create the immutable :class:`StepDefinition` from accumulated state."""
        return StepDefinition(
            id=self._step_id,
            step_method=self._handler_fn,
            compensate_method=self._compensate_fn,
            depends_on=list(self._depends_on),
            retry=self._retry,
            backoff_ms=self._backoff_ms,
            timeout_ms=self._timeout_ms,
            jitter=self._jitter,
            jitter_factor=self._jitter_factor,
            cpu_bound=self._cpu_bound,
        )


class SagaBuilder:
    """Fluent builder for programmatic saga definition.

    Use :meth:`step` to begin configuring a step, chain configuration
    methods, call ``.add()`` to finalise, and repeat.  Call :meth:`build`
    to validate and produce the :class:`SagaDefinition`.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._layer_concurrency: int = 0
        self._step_builders: list[StepBuilder] = []
        self._step_ids: set[str] = set()

    # ── Step creation ─────────────────────────────────────────

    def step(self, step_id: str) -> StepBuilder:
        """Begin configuring a new step with the given *step_id*.

        Args:
            step_id: Unique identifier for the step within this saga.

        Returns:
            A :class:`StepBuilder` for configuring the step.
        """
        return StepBuilder(step_id, self)

    # ── Saga-level configuration ──────────────────────────────

    def layer_concurrency(self, max_concurrent: int) -> SagaBuilder:
        """Set the maximum number of steps executed concurrently per layer.

        Args:
            max_concurrent: Concurrency limit.  ``0`` means unlimited.
        """
        self._layer_concurrency = max_concurrent
        return self

    # ── Build ─────────────────────────────────────────────────

    def build(self) -> SagaDefinition:
        """Validate and produce the final :class:`SagaDefinition`.

        Raises:
            SagaValidationError: If the saga has no steps, a step is missing
                a handler, dependencies reference nonexistent steps, or the
                dependency graph contains a cycle.
        """
        if not self._step_builders:
            msg = f"Saga '{self._name}' must have at least one step"
            raise SagaValidationError(msg)

        definition = SagaDefinition(
            name=self._name,
            bean=None,
            layer_concurrency=self._layer_concurrency,
        )

        # Build step definitions and validate handlers.
        for sb in self._step_builders:
            step_def = sb._build_definition()  # noqa: SLF001
            if step_def.step_method is None:
                msg = f"Step '{step_def.id}' in saga '{self._name}' must have a handler"
                raise SagaValidationError(msg)
            definition.steps[step_def.id] = step_def

        # Validate the dependency DAG (missing refs + cycle detection).
        self._validate_dag(definition)

        return definition

    # ── Internal helpers ──────────────────────────────────────

    def _add_step(self, step_builder: StepBuilder) -> None:
        """Register a finalised step builder (called by StepBuilder.add)."""
        step_id = step_builder._step_id  # noqa: SLF001
        if step_id in self._step_ids:
            msg = f"Step '{step_id}' already exists in saga '{self._name}'"
            raise SagaValidationError(msg)
        self._step_ids.add(step_id)
        self._step_builders.append(step_builder)

    @staticmethod
    def _validate_dag(definition: SagaDefinition) -> None:
        """Validate the step dependency graph.

        Checks:
        1. Every ``depends_on`` entry references an existing step id.
        2. The dependency graph is acyclic (Kahn's algorithm).

        Delegates error messages to match :class:`SagaValidationError`
        conventions used by :class:`SagaRegistry`.
        """
        step_ids = set(definition.steps.keys())

        # 1. Check all depends_on references exist.
        for step_id, step_def in definition.steps.items():
            for dep in step_def.depends_on:
                if dep not in step_ids:
                    msg = f"Step '{step_id}' in saga '{definition.name}' depends on '{dep}' which is nonexistent"
                    raise SagaValidationError(msg)

        # 2. Kahn's algorithm for cycle detection.
        in_degree: dict[str, int] = {sid: 0 for sid in step_ids}
        for step_def in definition.steps.values():
            for _dep in step_def.depends_on:
                in_degree[step_def.id] += 1

        queue: deque[str] = deque()
        for sid, degree in in_degree.items():
            if degree == 0:
                queue.append(sid)

        processed = 0
        while queue:
            current = queue.popleft()
            processed += 1
            for step_def in definition.steps.values():
                if current in step_def.depends_on:
                    in_degree[step_def.id] -= 1
                    if in_degree[step_def.id] == 0:
                        queue.append(step_def.id)

        if processed != len(step_ids):
            msg = f"Saga '{definition.name}' contains a dependency cycle (processed {processed}/{len(step_ids)} steps)"
            raise SagaValidationError(msg)
