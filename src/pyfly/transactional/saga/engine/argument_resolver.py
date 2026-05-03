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
"""Saga argument resolver — resolves ``Annotated`` parameters from execution context."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pyfly.transactional.saga.annotations import (
    FromCompensationResult,
    FromStep,
    Header,
    Input,
    SetVariable,
    Variable,
    _CompensationErrorSentinel,
    _HeadersSentinel,
    _VariablesSentinel,
)
from pyfly.transactional.saga.core.context import SagaContext

# Sentinel for "marker not recognised".
_UNRESOLVED = object()


class ArgumentResolver:
    """Resolves saga step method parameters from execution context.

    Inspects type hints (including ``Annotated`` extras) to determine how each
    parameter should be supplied.  The resolver handles:

    * ``SagaContext`` — injected by type match
    * ``Annotated[T, Input()]`` — entire step input
    * ``Annotated[T, Input("key")]`` — specific key from step input
    * ``Annotated[T, FromStep("id")]`` — result from a previous step
    * ``Annotated[str, Header("name")]`` — single header value
    * ``Annotated[dict, Headers]`` — full headers mapping
    * ``Annotated[T, Variable("name")]`` — single saga variable
    * ``Annotated[dict, Variables]`` — full variables mapping
    * ``Annotated[T, SetVariable("name")]`` — resolves to ``None`` (post-execution write)
    * ``Annotated[T, FromCompensationResult("id")]`` — compensation result
    * ``Annotated[Exception, CompensationError]`` — first compensation error
    """

    def resolve(
        self,
        func: Callable[..., Any],
        bean: Any,
        ctx: SagaContext,
        step_input: Any = None,
    ) -> dict[str, Any]:
        """Resolve all parameters for a step method.

        Parameters
        ----------
        func:
            The step method or function whose parameters should be resolved.
        bean:
            The saga instance that owns *func* (used only for ``self``
            detection).
        ctx:
            The current saga execution context.
        step_input:
            The input payload for this step, if any.

        Returns
        -------
        dict[str, Any]
            Dictionary mapping parameter names to their resolved values.

        Raises
        ------
        TypeError
            If a parameter cannot be resolved from any known source.
        """
        hints = get_type_hints(func, include_extras=True)
        sig = inspect.signature(func)

        resolved: dict[str, Any] = {}

        for name in sig.parameters:
            if name == "self":
                continue

            hint = hints.get(name)
            if hint is None:
                raise TypeError(
                    f"Cannot resolve parameter '{name}' — no type hint found on {func.__qualname__}",
                )

            value = self._resolve_parameter(name, hint, ctx, step_input)
            resolved[name] = value

        return resolved

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_parameter(
        self,
        name: str,
        hint: Any,
        ctx: SagaContext,
        step_input: Any,
    ) -> Any:
        """Resolve a single parameter from its type hint."""
        # 1. Direct SagaContext type match (no Annotated wrapper needed).
        if self._is_saga_context(hint):
            return ctx

        # 2. Annotated[T, marker] — inspect extras for known markers.
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            # args[0] is the base type, args[1:] are the extras
            base_type = args[0]

            # Check base type for SagaContext in case of Annotated[SagaContext, ...]
            if self._is_saga_context(base_type):
                return ctx

            for extra in args[1:]:
                result = self._resolve_marker(extra, ctx, step_input)
                if result is not _UNRESOLVED:
                    return result

        # 3. Cannot resolve — raise.
        raise TypeError(
            f"Cannot resolve parameter '{name}' — no matching injection marker found in type hint: {hint}",
        )

    def _resolve_marker(
        self,
        marker: Any,
        ctx: SagaContext,
        step_input: Any,
    ) -> Any:
        """Attempt to resolve a value from a single annotation marker.

        Returns ``_UNRESOLVED`` if the marker is not recognised.
        """
        # Input() or Input("key")
        if isinstance(marker, Input):
            if marker.key is None:
                return step_input
            return self._extract_key(step_input, marker.key)

        # FromStep("step-id")
        if isinstance(marker, FromStep):
            return ctx.step_results.get(marker.step_id)

        # Header("name")
        if isinstance(marker, Header):
            return ctx.headers.get(marker.name)

        # Headers sentinel — full dict
        if isinstance(marker, _HeadersSentinel):
            return dict(ctx.headers)

        # Variable("name")
        if isinstance(marker, Variable):
            return ctx.variables.get(marker.name)

        # Variables sentinel — full dict
        if isinstance(marker, _VariablesSentinel):
            return dict(ctx.variables)

        # SetVariable("name") — resolved to None; engine writes post-execution
        if isinstance(marker, SetVariable):
            return None

        # FromCompensationResult("step-id")
        if isinstance(marker, FromCompensationResult):
            return ctx.compensation_results.get(marker.step_id)

        # CompensationError sentinel — first error or None
        if isinstance(marker, _CompensationErrorSentinel):
            if ctx.compensation_errors:
                return next(iter(ctx.compensation_errors.values()))
            return None

        return _UNRESOLVED

    @staticmethod
    def _is_saga_context(hint: Any) -> bool:
        """Return ``True`` if *hint* resolves to :class:`SagaContext`."""
        # With ``from __future__ import annotations``, hints may be strings.
        if isinstance(hint, str):
            return hint == "SagaContext"
        return hint is SagaContext

    @staticmethod
    def _extract_key(obj: Any, key: str) -> Any:
        """Extract *key* from *obj* — supports dict subscript and attribute access."""
        if isinstance(obj, dict):
            return obj[key]
        return getattr(obj, key)
