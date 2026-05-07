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
"""Parameter-injection annotations and resolver for orchestration steps.

Mirrors the Java engine's ``core/argument`` package.  Step methods declare
their dependencies through ``Annotated[T, Input(...)]``,
``Annotated[T, FromStep("step-id")]`` etc.  At call time the
:class:`ArgumentResolver` inspects the signature and resolves each parameter
from the :class:`ExecutionContext`.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from pyfly.transactional.core.context import ExecutionContext

# --- Annotation markers -------------------------------------------------------


@dataclass(frozen=True)
class Input:
    """Inject the saga / workflow / TCC input.

    If ``field`` is given, the corresponding attribute / dict key of the
    input payload is extracted instead of the whole input.
    """

    field: str | None = None


@dataclass(frozen=True)
class FromStep:
    """Inject the result of a previously executed step."""

    step_id: str
    field: str | None = None


@dataclass(frozen=True)
class Variable:
    """Inject a context variable previously set via :class:`SetVariable`."""

    name: str


@dataclass(frozen=True)
class SetVariable:
    """Marker that a return value (or named parameter) sets a context variable.

    Use either as a parameter annotation (when applied to a ``dict[str, Any]``
    parameter, the engine *writes* the dict back to the context) or as a
    return-type annotation (then the entire return value is stored under
    ``name``).
    """

    name: str | None = None


@dataclass(frozen=True)
class Variables:
    """Inject the entire variable map as ``dict[str, Any]``."""


@dataclass(frozen=True)
class CorrelationId:
    """Inject the execution's correlation id."""


@dataclass(frozen=True)
class Header:
    """Inject a single header by name."""

    name: str


@dataclass(frozen=True)
class Headers:
    """Inject the full ``dict[str, str]`` of headers."""


@dataclass(frozen=True)
class FromCompensationResult:
    """Inject the result of an earlier compensation step (saga only)."""

    step_id: str


@dataclass(frozen=True)
class CompensationError:
    """Inject the error that triggered the current compensation."""


@dataclass(frozen=True)
class Required:
    """Assert the resolved value is non-``None``."""


@dataclass(frozen=True)
class FromTry:
    """Inject the Try-phase result of a TCC participant."""

    participant_id: str | None = None  # ``None`` = same participant


_KNOWN_MARKERS = (
    Input,
    FromStep,
    Variable,
    SetVariable,
    Variables,
    CorrelationId,
    Header,
    Headers,
    FromCompensationResult,
    CompensationError,
    Required,
    FromTry,
)


# --- Resolver -----------------------------------------------------------------


@dataclass
class ResolvedParameter:
    """Per-parameter resolution metadata cached in :class:`ArgumentResolver`."""

    name: str
    marker: Any | None
    required: bool


class ArgumentResolver:
    """Inspect a step / participant method and supply its arguments at runtime."""

    def __init__(self) -> None:
        self._cache: dict[Callable[..., Any], list[ResolvedParameter]] = {}

    def resolve(
        self,
        method: Callable[..., Any],
        ctx: ExecutionContext,
        *,
        compensation_error: BaseException | None = None,
        compensation_results: dict[str, Any] | None = None,
        current_participant_id: str | None = None,
        skip_first: bool = False,
    ) -> dict[str, Any]:
        """Build the kwargs dict for a call to *method* against *ctx*.

        Args:
            method: Target callable.
            ctx: Active execution context.
            compensation_error: When invoking compensation, the original error.
            compensation_results: Map of step-id → compensation result (saga).
            current_participant_id: Used for ``@FromTry()`` with no explicit id.
            skip_first: Skip the first parameter (used for unbound methods).

        Returns:
            Dictionary of keyword arguments ready to splat into ``method(...)``.
        """
        plans = self._plans_for(method, skip_first=skip_first)
        kwargs: dict[str, Any] = {}
        for plan in plans:
            value = self._resolve_one(plan, ctx, compensation_error, compensation_results, current_participant_id)
            if plan.required and value is None:
                msg = f"required parameter '{plan.name}' resolved to None"
                raise ValueError(msg)
            kwargs[plan.name] = value
        return kwargs

    # -- internals ----------------------------------------------------------

    def _plans_for(self, method: Callable[..., Any], *, skip_first: bool) -> list[ResolvedParameter]:
        cached = self._cache.get(method)
        if cached is not None:
            return cached

        sig = inspect.signature(method)
        plans: list[ResolvedParameter] = []
        params = list(sig.parameters.values())
        if skip_first and params and params[0].name in {"self", "cls"} or skip_first and params:
            params = params[1:]

        try:
            type_hints = typing.get_type_hints(method, include_extras=True)
        except Exception:
            type_hints = {}

        for param in params:
            marker, required = _extract_marker(type_hints.get(param.name, param.annotation))
            plans.append(ResolvedParameter(name=param.name, marker=marker, required=required))

        self._cache[method] = plans
        return plans

    @staticmethod
    def _resolve_one(
        plan: ResolvedParameter,
        ctx: ExecutionContext,
        compensation_error: BaseException | None,
        compensation_results: dict[str, Any] | None,
        current_participant_id: str | None,
    ) -> Any:
        m = plan.marker
        if m is None:
            # No marker → default to context for ExecutionContext-typed params.
            return ctx
        if isinstance(m, Input):
            return _read_field(ctx.input, m.field)
        if isinstance(m, FromStep):
            return _read_field(ctx.get_step_result(m.step_id), m.field)
        if isinstance(m, Variable):
            return ctx.get_variable(m.name)
        if isinstance(m, Variables):
            return ctx.get_all_variables()
        if isinstance(m, CorrelationId):
            return ctx.correlation_id
        if isinstance(m, Header):
            return ctx.get_header(m.name)
        if isinstance(m, Headers):
            return dict(ctx.headers)
        if isinstance(m, FromCompensationResult):
            return (compensation_results or {}).get(m.step_id)
        if isinstance(m, CompensationError):
            return compensation_error
        if isinstance(m, FromTry):
            pid = m.participant_id or current_participant_id
            return ctx.get_try_result(pid) if pid else None
        if isinstance(m, SetVariable):
            # Provide a dict the step can mutate; engine will copy into ctx after invocation.
            return {}
        return ctx


def extract_marker(annotation: Any) -> tuple[Any | None, bool]:
    """Pull a known marker out of an ``Annotated[...]``; track ``Required``."""
    if get_origin(annotation) is Annotated:
        marker: Any | None = None
        required = False
        for meta in get_args(annotation)[1:]:
            if isinstance(meta, Required):
                required = True
            elif isinstance(meta, _KNOWN_MARKERS):
                marker = meta
        return marker, required
    if isinstance(annotation, _KNOWN_MARKERS):
        return annotation, False
    return None, False


# Backwards-compatible alias for tests written against the private name.
_extract_marker = extract_marker


def _read_field(value: Any, field: str | None) -> Any:
    if field is None or value is None:
        return value
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)
