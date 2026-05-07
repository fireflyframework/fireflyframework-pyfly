# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the ArgumentResolver and Annotated[...] markers."""

from __future__ import annotations

from typing import Annotated

import pytest

from pyfly.transactional.core.argument import (
    ArgumentResolver,
    CorrelationId,
    FromCompensationResult,
    FromStep,
    Header,
    Headers,
    Input,
    Required,
    Variable,
    Variables,
)
from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.model import ExecutionPattern


def _ctx(input_data: object = None, headers: dict[str, str] | None = None) -> ExecutionContext:
    ctx = ExecutionContext(
        name="t",
        pattern=ExecutionPattern.SAGA,
        input=input_data or {"name": "alice", "age": 30},
        headers=headers or {},
    )
    return ctx


class TestArgumentResolver:
    def test_input_full_payload(self) -> None:
        resolver = ArgumentResolver()

        def step(payload: Annotated[dict, Input()]) -> None: ...

        ctx = _ctx({"x": 1})
        kwargs = resolver.resolve(step, ctx)
        assert kwargs["payload"] == {"x": 1}

    def test_input_field(self) -> None:
        resolver = ArgumentResolver()

        def step(name: Annotated[str, Input(field="name")]) -> None: ...

        kwargs = resolver.resolve(step, _ctx())
        assert kwargs["name"] == "alice"

    @pytest.mark.asyncio
    async def test_from_step_result(self) -> None:
        resolver = ArgumentResolver()

        def step(prev: Annotated[dict, FromStep("first")]) -> None: ...

        ctx = _ctx()
        await ctx.record_step_success("first", {"answer": 42}, 1.0)
        kwargs = resolver.resolve(step, ctx)
        assert kwargs["prev"] == {"answer": 42}

    @pytest.mark.asyncio
    async def test_variable(self) -> None:
        resolver = ArgumentResolver()

        def step(token: Annotated[str, Variable("token")]) -> None: ...

        ctx = _ctx()
        await ctx.set_variable("token", "abc")
        assert resolver.resolve(step, ctx)["token"] == "abc"

    @pytest.mark.asyncio
    async def test_variables_dict(self) -> None:
        resolver = ArgumentResolver()

        def step(all_vars: Annotated[dict, Variables()]) -> None: ...

        ctx = _ctx()
        await ctx.set_variable("a", 1)
        kwargs = resolver.resolve(step, ctx)
        assert kwargs["all_vars"] == {"a": 1}

    def test_correlation_id(self) -> None:
        resolver = ArgumentResolver()

        def step(cid: Annotated[str, CorrelationId()]) -> None: ...

        ctx = _ctx()
        assert resolver.resolve(step, ctx)["cid"] == ctx.correlation_id

    def test_header(self) -> None:
        resolver = ArgumentResolver()

        def step(auth: Annotated[str, Header("auth")]) -> None: ...

        ctx = _ctx(headers={"auth": "Bearer xyz"})
        assert resolver.resolve(step, ctx)["auth"] == "Bearer xyz"

    def test_headers(self) -> None:
        resolver = ArgumentResolver()

        def step(h: Annotated[dict, Headers()]) -> None: ...

        ctx = _ctx(headers={"a": "1", "b": "2"})
        assert resolver.resolve(step, ctx)["h"] == {"a": "1", "b": "2"}

    def test_from_compensation_result(self) -> None:
        resolver = ArgumentResolver()

        def step(prev: Annotated[dict, FromCompensationResult("x")]) -> None: ...

        kwargs = resolver.resolve(step, _ctx(), compensation_results={"x": {"ok": True}})
        assert kwargs["prev"] == {"ok": True}

    def test_required_raises_when_none(self) -> None:
        resolver = ArgumentResolver()

        def step(x: Annotated[str, Variable("missing"), Required()]) -> None: ...

        with pytest.raises(ValueError, match="required parameter"):
            resolver.resolve(step, _ctx())
