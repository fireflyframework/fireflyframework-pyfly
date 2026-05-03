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
"""Saga registry — discovery, indexing and DAG validation of saga beans."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.step_definition import StepDefinition


class SagaValidationError(Exception):
    """Raised when a saga definition fails structural validation.

    Typical causes include cyclic dependencies between steps or references
    to step ids that do not exist within the saga.
    """


class SagaRegistry:
    """Discovery and indexing service for ``@saga``-decorated beans.

    Scans bean classes for ``__pyfly_saga__`` and ``__pyfly_saga_step__``
    metadata, resolves compensation methods by name lookup, validates the
    step dependency graph and stores the resulting :class:`SagaDefinition`.
    """

    def __init__(self) -> None:
        self._sagas: dict[str, SagaDefinition] = {}

    # -- Public API ----------------------------------------------------------

    def register_from_bean(self, bean: Any) -> SagaDefinition:
        """Register a saga from a decorated bean instance.

        Inspects the bean's class for ``__pyfly_saga__`` metadata, discovers
        all methods carrying ``__pyfly_saga_step__`` metadata, resolves
        compensation methods by name and validates the dependency DAG.

        Args:
            bean: An instance of a ``@saga``-decorated class.

        Returns:
            The fully populated :class:`SagaDefinition`.

        Raises:
            SagaValidationError: If dependencies reference missing step ids
                or if the dependency graph contains a cycle.
        """
        cls = type(bean)
        saga_meta: dict[str, Any] = getattr(cls, "__pyfly_saga__", None)  # type: ignore[assignment]
        if saga_meta is None:
            msg = f"{cls.__qualname__} is not decorated with @saga"
            raise SagaValidationError(msg)

        saga_name: str = saga_meta["name"]
        layer_concurrency: int = saga_meta.get("layer_concurrency", 0)

        definition = SagaDefinition(
            name=saga_name,
            bean=bean,
            layer_concurrency=layer_concurrency,
        )

        # Discover steps by iterating over *class* attributes so that we
        # see the decorated wrappers (which carry __pyfly_saga_step__).
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if attr is None:
                continue
            step_meta: dict[str, Any] | None = getattr(attr, "__pyfly_saga_step__", None)
            if step_meta is None:
                continue

            step_id: str = step_meta["id"]
            compensate_name: str | None = step_meta.get("compensate")
            depends_on: list[str] = step_meta.get("depends_on", [])

            # Resolve the *unbound* step method on the class so that
            # ``StepInvoker._call`` can pass the bean as the first positional
            # argument (``self``).  This is consistent with how the TCC
            # registry resolves participant methods.
            step_method: Callable[..., Any] | None = getattr(cls, attr_name, None)

            # Resolve the compensation method by name lookup on the class.
            compensate_method: Callable[..., Any] | None = None
            if compensate_name is not None:
                compensate_method = getattr(cls, compensate_name, None)

            step_def = StepDefinition(
                id=step_id,
                step_method=step_method,
                compensate_name=compensate_name,
                compensate_method=compensate_method,
                depends_on=list(depends_on),
                retry=step_meta.get("retry", 0),
                backoff_ms=step_meta.get("backoff_ms", 0),
                timeout_ms=step_meta.get("timeout_ms", 0),
                jitter=step_meta.get("jitter", False),
                jitter_factor=step_meta.get("jitter_factor", 0.0),
                cpu_bound=step_meta.get("cpu_bound", False),
                idempotency_key=step_meta.get("idempotency_key"),
                compensation_retry=step_meta.get("compensation_retry"),
                compensation_backoff_ms=step_meta.get("compensation_backoff_ms"),
                compensation_timeout_ms=step_meta.get("compensation_timeout_ms"),
                compensation_critical=step_meta.get("compensation_critical", False),
            )
            definition.steps[step_id] = step_def

        # Validate the dependency DAG.
        self._validate_dag(definition)

        self._sagas[saga_name] = definition
        return definition

    def get(self, name: str) -> SagaDefinition | None:
        """Look up a saga definition by name.

        Args:
            name: The saga name supplied to ``@saga(name=...)``.

        Returns:
            The :class:`SagaDefinition` if registered, otherwise ``None``.
        """
        return self._sagas.get(name)

    def get_all(self) -> list[SagaDefinition]:
        """Return all registered saga definitions.

        Returns:
            A list of all :class:`SagaDefinition` instances currently
            registered in this registry.
        """
        return list(self._sagas.values())

    # -- DAG validation ------------------------------------------------------

    @staticmethod
    def _validate_dag(definition: SagaDefinition) -> None:
        """Validate the step dependency graph using Kahn's algorithm.

        Checks that:
        1. Every ``depends_on`` entry references an existing step id.
        2. The dependency graph is acyclic (topological sort succeeds).

        Args:
            definition: The saga definition whose steps to validate.

        Raises:
            SagaValidationError: On missing dependencies or cycles.
        """
        step_ids = set(definition.steps.keys())

        # 1. Check all depends_on references exist.
        for step_id, step_def in definition.steps.items():
            for dep in step_def.depends_on:
                if dep not in step_ids:
                    msg = f"Step '{step_id}' in saga '{definition.name}' depends on '{dep}' which is nonexistent"
                    raise SagaValidationError(msg)

        # 2. Kahn's algorithm for topological sort / cycle detection.
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
            # Find all steps that depend on `current` and decrement their
            # in-degree.
            for step_def in definition.steps.values():
                if current in step_def.depends_on:
                    in_degree[step_def.id] -= 1
                    if in_degree[step_def.id] == 0:
                        queue.append(step_def.id)

        if processed != len(step_ids):
            msg = f"Saga '{definition.name}' contains a dependency cycle (processed {processed}/{len(step_ids)} steps)"
            raise SagaValidationError(msg)
