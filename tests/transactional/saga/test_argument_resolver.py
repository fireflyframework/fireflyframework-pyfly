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
"""Tests for saga argument resolver — Annotated parameter injection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

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
)
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver


@dataclass
class OrderInput:
    order_id: str
    amount: float


class _FakeBean:
    """Dummy saga class used as the ``self`` receiver in test methods."""


@pytest.fixture
def resolver() -> ArgumentResolver:
    return ArgumentResolver()


@pytest.fixture
def ctx() -> SagaContext:
    return SagaContext(
        saga_name="test-saga",
        headers={"x-request-id": "req-1", "x-tenant": "acme"},
        variables={"retry_count": 3, "region": "us-east-1"},
        step_results={"validate": {"valid": True}, "reserve": 42},
        compensation_results={"reserve": "refunded"},
        compensation_errors={"reserve": RuntimeError("boom")},
    )


@pytest.fixture
def bean() -> _FakeBean:
    return _FakeBean()


# ── Input markers ────────────────────────────────────────────


class TestInputResolution:
    def test_entire_input(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[T, Input()] injects the whole step_input object."""

        def step(self: Any, data: Annotated[OrderInput, Input()]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx, step_input=OrderInput("o1", 9.99))
        assert resolved["data"] == OrderInput("o1", 9.99)

    def test_input_with_key_dict(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[T, Input('key')] extracts a key from a dict input."""

        def step(self: Any, oid: Annotated[str, Input("order_id")]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx, step_input={"order_id": "o1", "amount": 9.99})
        assert resolved["oid"] == "o1"

    def test_input_with_key_attr(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[T, Input('key')] extracts an attribute from a dataclass input."""

        def step(self: Any, amt: Annotated[float, Input("amount")]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx, step_input=OrderInput("o1", 9.99))
        assert resolved["amt"] == 9.99


# ── FromStep marker ──────────────────────────────────────────


class TestFromStepResolution:
    def test_from_step(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[T, FromStep('id')] pulls from ctx.step_results."""

        def step(self: Any, result: Annotated[dict, FromStep("validate")]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["result"] == {"valid": True}


# ── Header markers ───────────────────────────────────────────


class TestHeaderResolution:
    def test_single_header(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[str, Header('name')] injects a single header."""

        def step(self: Any, req_id: Annotated[str, Header("x-request-id")]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["req_id"] == "req-1"

    def test_all_headers(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[dict, Headers] injects the full headers dict."""

        def step(self: Any, hdrs: Annotated[dict, Headers]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["hdrs"] == {"x-request-id": "req-1", "x-tenant": "acme"}


# ── Variable markers ─────────────────────────────────────────


class TestVariableResolution:
    def test_single_variable(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[T, Variable('name')] injects a single variable."""

        def step(self: Any, retries: Annotated[int, Variable("retry_count")]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["retries"] == 3

    def test_all_variables(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[dict, Variables] injects the full variables dict."""

        def step(self: Any, vs: Annotated[dict, Variables]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["vs"] == {"retry_count": 3, "region": "us-east-1"}


# ── SagaContext by type ──────────────────────────────────────


class TestSagaContextResolution:
    def test_saga_context_by_type(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """A parameter typed as SagaContext is injected directly."""

        def step(self: Any, context: SagaContext) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["context"] is ctx


# ── SetVariable marker ───────────────────────────────────────


class TestSetVariableResolution:
    def test_set_variable_returns_none(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[T, SetVariable('name')] resolves to None (post-execution)."""

        def step(self: Any, out: Annotated[str, SetVariable("output_key")]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["out"] is None


# ── FromCompensationResult marker ────────────────────────────


class TestFromCompensationResultResolution:
    def test_from_compensation_result(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[T, FromCompensationResult('id')] pulls from compensation_results."""

        def step(self: Any, cr: Annotated[str, FromCompensationResult("reserve")]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert resolved["cr"] == "refunded"


# ── CompensationError sentinel ───────────────────────────────


class TestCompensationErrorResolution:
    def test_compensation_error(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Annotated[Exception, CompensationError] injects first compensation error."""

        def step(self: Any, err: Annotated[Exception, CompensationError]) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert isinstance(resolved["err"], RuntimeError)
        assert str(resolved["err"]) == "boom"

    def test_compensation_error_none_when_absent(self, resolver: ArgumentResolver, bean: _FakeBean) -> None:
        """CompensationError resolves to None when no errors present."""
        empty_ctx = SagaContext(saga_name="test")

        def step(self: Any, err: Annotated[Exception | None, CompensationError]) -> None: ...

        resolved = resolver.resolve(step, bean, empty_ctx)
        assert resolved["err"] is None


# ── Self skipping ────────────────────────────────────────────


class TestSelfSkipping:
    def test_skip_self(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """The ``self`` parameter is never included in the resolved dict."""

        def step(self: Any, context: SagaContext) -> None: ...

        resolved = resolver.resolve(step, bean, ctx)
        assert "self" not in resolved


# ── Unknown parameter ────────────────────────────────────────


class TestUnknownParameter:
    def test_unknown_raises(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """An unannotated, unrecognised parameter raises TypeError."""

        def step(self: Any, mystery: str) -> None: ...

        with pytest.raises(TypeError, match="mystery"):
            resolver.resolve(step, bean, ctx)


# ── Multiple parameters combined ─────────────────────────────


class TestCombinedResolution:
    def test_multiple_markers(self, resolver: ArgumentResolver, ctx: SagaContext, bean: _FakeBean) -> None:
        """Multiple parameters with different markers resolve correctly."""

        def step(
            self: Any,
            data: Annotated[OrderInput, Input()],
            prev: Annotated[dict, FromStep("validate")],
            context: SagaContext,
            req_id: Annotated[str, Header("x-request-id")],
            region: Annotated[str, Variable("region")],
        ) -> None: ...

        inp = OrderInput("o1", 9.99)
        resolved = resolver.resolve(step, bean, ctx, step_input=inp)

        assert resolved["data"] == inp
        assert resolved["prev"] == {"valid": True}
        assert resolved["context"] is ctx
        assert resolved["req_id"] == "req-1"
        assert resolved["region"] == "us-east-1"
