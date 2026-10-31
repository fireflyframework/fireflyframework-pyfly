# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Awaited per-attempt audit boundaries, independent of best-effort AgentEvents."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from pyfly.audit import AuditEffect, AuditEffectUncertain, AuditError, AuditOperation, AuditRecorder, AuditUsage

if TYPE_CHECKING:
    from pyfly.agentic.runtime import AgenticRuntime


def _hint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _require_model_adapter(model: object) -> None:
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    from pydantic_ai.models.test import TestModel

    if type(model) not in {FunctionModel, TestModel, OpenAIChatModel, OpenAIResponsesModel}:
        raise AuditError("Automatic model audit requires a supported per-attempt adapter; no request was admitted")


def require_execution_loop(runtime: AgenticRuntime) -> None:
    if runtime._audit_recorder("agentic") is not None:
        owner = runtime._application_loop
        if owner is not None and owner is not asyncio.get_running_loop():
            raise AuditError("Automatic Agentic audit requires the host application's owning async event loop")


class _Invocation:
    def __init__(self, recorder: AuditRecorder) -> None:
        self.recorder = recorder
        self.fenced = False
        self.pipeline_stack: AsyncExitStack | None = None

    def check(self) -> None:
        if self.fenced:
            raise AuditEffectUncertain("A prior effect may have occurred; automatic execution is fenced") from None

    @asynccontextmanager
    async def effect(
        self, action: str, resource: str, *, admission_key: str | None = None
    ) -> AsyncIterator[AuditEffect]:
        self.check()
        try:
            async with self.recorder.effect(
                AuditOperation(
                    module="agentic", action=action, resource_kind=action.split(".")[0], resource_key=_hint(resource)
                ),
                admission_key=admission_key or uuid4().hex,
            ) as effect:
                yield effect
        except (AuditEffectUncertain, asyncio.CancelledError):
            self.fenced = True
            raise


_invocation: ContextVar[_Invocation | None] = ContextVar("pyfly.agentic.audit.invocation", default=None)


def current_audit_invocation() -> _Invocation | None:
    from pyfly.agentic._runtime_context import selected_runtime

    invocation = _invocation.get()
    runtime = selected_runtime()
    if runtime is None:
        return invocation
    recorder = runtime._audit_recorder("agentic")
    if recorder is None:
        return None
    if invocation is None or invocation.recorder is not recorder:
        return _Invocation(recorder)
    return invocation


@asynccontextmanager
async def agent_execution(runtime: AgenticRuntime) -> AsyncIterator[None]:
    recorder = runtime._audit_recorder("agentic")
    if recorder is None:
        yield
        return
    invocation = _invocation.get()
    if invocation is None or invocation.recorder is not recorder:
        invocation = _Invocation(recorder)
    invocation.check()
    token = _invocation.set(invocation)
    try:
        yield
        invocation.check()
    except Exception:
        if invocation.fenced:
            raise AuditEffectUncertain("A prior effect may have occurred; automatic execution is fenced") from None
        raise
    finally:
        _invocation.reset(token)


@asynccontextmanager
async def tool_effect(name: str) -> AsyncIterator[None]:
    invocation = current_audit_invocation()
    if invocation is None:
        yield
        return
    token = _invocation.set(invocation)
    try:
        async with invocation.effect("tool.call", name):
            yield
    finally:
        _invocation.reset(token)


def pipeline_recording[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(function)
    async def recorded(*args: P.args, **kwargs: P.kwargs) -> R:
        self: Any = args[0]
        recorder = self._audit_recorder("agentic")
        parent = current_audit_invocation()
        if recorder is None and parent is None:
            return await function(*args, **kwargs)
        if parent is not None:
            parent.check()
        invocation = _Invocation(recorder if recorder is not None else cast(_Invocation, parent).recorder)
        if self._dag.is_cyclic() or self._routers:
            raise AuditError("Automatic audit does not support cyclic or dynamic pipeline replay identities")
        if (
            kwargs.get("run_id") is not None
            and len(args) == 1
            and all(kwargs.get(field) is None for field in ("context", "inputs", "state"))
        ):
            raise AuditError("Audited checkpoint resume requires an explicit host reconciliation boundary")
        token = _invocation.set(invocation)
        try:
            async with AsyncExitStack() as stack:
                invocation.pipeline_stack = stack
                result = await function(*args, **kwargs)
                invocation.check()
                return result
        finally:
            if invocation.fenced and parent is not None and invocation.recorder is parent.recorder:
                parent.fenced = True
            invocation.pipeline_stack = None
            _invocation.reset(token)

    return recorded


async def pipeline_admitted(name: str, run_id: str) -> None:
    invocation = _invocation.get()
    if invocation is not None and invocation.pipeline_stack is not None:
        await invocation.pipeline_stack.enter_async_context(
            invocation.effect("pipeline.run", name, admission_key=_hint(f"pipeline:{run_id}"))
        )


@asynccontextmanager
async def pipeline_node_effect(name: str, node_id: str) -> AsyncIterator[None]:
    invocation = _invocation.get()
    if invocation is None:
        yield
        return
    async with invocation.effect("pipeline.node", f"{name}:{node_id}"):
        yield


def _usage(response: ModelResponse) -> AuditUsage:
    usage = response.usage
    if usage.input_tokens or usage.output_tokens:
        return AuditUsage(
            status="measured", requests=1, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
        )
    return AuditUsage(status="not_reported", requests=1)


class _RecordedModel(WrapperModel):
    def __init__(self, model: Model, invocation: _Invocation) -> None:
        super().__init__(model)
        self._invocation = invocation

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        settings, parameters = self.wrapped.prepare_request(model_settings, model_request_parameters)
        async with self._invocation.effect("model.request", self.wrapped.model_name) as effect:
            response = await self.wrapped.request(messages, settings, parameters)
            effect.enrich(usage=_usage(response))
            return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        settings, parameters = self.wrapped.prepare_request(model_settings, model_request_parameters)
        async with (
            self._invocation.effect("model.request", self.wrapped.model_name) as effect,
            self.wrapped.request_stream(messages, settings, parameters, run_context) as response,
        ):
            yield response
            result = response.get()
            if result.state not in {"complete", "suspended"}:
                raise AuditEffectUncertain("The model stream did not confirm completion; do not retry automatically")
            effect.enrich(usage=_usage(result))


class AuditAgentCapability(AbstractCapability[Any]):
    def __init__(
        self,
        runtime: AgenticRuntime,
        agent_name: str,
        *,
        native_handlers: tuple[object, ...],
        native_tool_sources: bool,
    ) -> None:
        self.runtime, self.agent_name = runtime, agent_name
        self.native_handlers, self.native_tool_sources = native_handlers, native_tool_sources

    def _require_tool_adapters(self, ctx: RunContext[Any]) -> None:
        from pydantic_ai.toolsets import FunctionToolset

        message = "Automatic tool audit requires native AgentTool adapters; raw SDK tools and toolsets are unsupported"
        if not self.native_tool_sources or ctx.agent is None:
            raise AuditError(message)
        for toolset in ctx.agent.toolsets:
            if not isinstance(toolset, FunctionToolset) or any(
                not any(tool.function is handler for handler in self.native_handlers) for tool in toolset.tools.values()
            ):
                raise AuditError(message)

    async def wrap_run(
        self, ctx: RunContext[Any], *, handler: Callable[[], Awaitable[AgentRunResult[Any]]]
    ) -> AgentRunResult[Any]:
        recorder = self.runtime._audit_recorder("agentic")
        if recorder is None:
            return await handler()
        require_execution_loop(self.runtime)
        _require_model_adapter(ctx.model)
        self._require_tool_adapters(ctx)
        if not ctx.run_id:
            raise AuditError("Automatic agent audit requires a native run command identity")
        invocation = _invocation.get()
        if invocation is None or invocation.recorder is not recorder:
            invocation = _Invocation(recorder)
        invocation.check()
        token = _invocation.set(invocation)
        try:
            async with invocation.effect("agent.run", self.agent_name, admission_key=_hint(f"agent:{ctx.run_id}")):
                result = await handler()
                invocation.check()
                return result
        finally:
            _invocation.reset(token)

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        invocation = _invocation.get()
        if invocation is None:
            return request_context
        invocation.check()
        model = request_context.model
        _require_model_adapter(model)
        settings = cast(ModelSettings, {**(request_context.model_settings or {}), "max_retries": 0})
        return replace(request_context, model=_RecordedModel(model, invocation), model_settings=settings)

    async def wrap_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        handler: Callable[[ModelRequestContext], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        invocation = _invocation.get()
        if invocation is not None:
            invocation.check()
        response = await handler(request_context)
        if invocation is not None:
            invocation.check()
        return response
