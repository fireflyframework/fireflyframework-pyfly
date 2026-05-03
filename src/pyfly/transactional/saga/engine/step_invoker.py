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
"""Saga step invoker — invoke step/compensation methods with resolved arguments."""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pyfly.transactional.saga.annotations import SetVariable
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.registry.step_definition import StepDefinition


class StepInvoker:
    """Invokes saga step and compensation methods with resolved arguments."""

    def __init__(self, argument_resolver: ArgumentResolver) -> None:
        self._argument_resolver = argument_resolver

    async def invoke_step(
        self,
        step_def: StepDefinition,
        bean: Any,
        ctx: SagaContext,
        step_input: Any = None,
    ) -> Any:
        """Invoke a step method with resolved arguments.

        Returns the step result.
        For cpu_bound steps, runs in a thread pool executor.
        """
        method = step_def.step_method
        if method is None:
            raise ValueError(f"Step '{step_def.id}' has no step method defined.")

        kwargs = self._argument_resolver.resolve(method, bean, ctx, step_input)
        result = await self._call(method, bean, kwargs, cpu_bound=step_def.cpu_bound)

        # Handle SetVariable markers: store result in context.
        self._apply_set_variables(method, result, ctx)

        return result

    async def invoke_compensation(
        self,
        step_def: StepDefinition,
        bean: Any,
        ctx: SagaContext,
        step_input: Any = None,
    ) -> Any:
        """Invoke a compensation method with resolved arguments.

        Returns the compensation result.
        Raises if no compensation method is defined.
        """
        method = step_def.compensate_method
        if method is None:
            raise ValueError(f"Step '{step_def.id}' has no compensation method defined.")

        kwargs = self._argument_resolver.resolve(method, bean, ctx, step_input)
        return await self._call(method, bean, kwargs, cpu_bound=False)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _call(
        method: Callable[..., Any],
        bean: Any,
        kwargs: dict[str, Any],
        *,
        cpu_bound: bool,
    ) -> Any:
        """Call *method* with *bean* as ``self`` and the resolved *kwargs*.

        For ``cpu_bound`` synchronous functions, offloads execution to a
        thread pool executor via :meth:`asyncio.get_event_loop().run_in_executor`.
        """
        if inspect.iscoroutinefunction(method):
            return await method(bean, **kwargs)

        if cpu_bound:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                functools.partial(method, bean, **kwargs),
            )

        # Synchronous, non-cpu-bound: call directly.
        return method(bean, **kwargs)

    @staticmethod
    def _apply_set_variables(
        method: Callable[..., Any],
        result: Any,
        ctx: SagaContext,
    ) -> None:
        """Scan *method* parameters for ``SetVariable`` markers and store *result*."""
        hints = get_type_hints(method, include_extras=True)

        for hint in hints.values():
            if get_origin(hint) is not Annotated:
                continue
            for extra in get_args(hint)[1:]:
                if isinstance(extra, SetVariable):
                    ctx.set_variable(extra.name, result)
