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
"""Tests for TCC engine — argument resolver, participant invoker, orchestrator, and engine."""

from __future__ import annotations

from typing import Annotated, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyfly.transactional.saga.annotations import Input
from pyfly.transactional.tcc.annotations import (
    FromTry,
    cancel_method,
    confirm_method,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.core.context import TccContext
from pyfly.transactional.tcc.core.phase import TccPhase
from pyfly.transactional.tcc.core.result import TccResult
from pyfly.transactional.tcc.engine.argument_resolver import TccArgumentResolver
from pyfly.transactional.tcc.engine.execution_orchestrator import (
    TccExecutionOrchestrator,
)
from pyfly.transactional.tcc.engine.participant_invoker import TccParticipantInvoker
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from pyfly.transactional.tcc.registry.participant_definition import (
    ParticipantDefinition,
)
from pyfly.transactional.tcc.registry.tcc_definition import TccDefinition
from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry

# ── Helpers ──────────────────────────────────────────────────


class _FakeBean:
    """Dummy TCC bean."""


def _make_participant_def(
    pid: str,
    order: int = 0,
    timeout_ms: int = 0,
    optional: bool = False,
    has_confirm: bool = True,
    has_cancel: bool = True,
) -> ParticipantDefinition:
    """Create a minimal ParticipantDefinition with dummy methods."""

    @tcc_participant(id=pid, order=order)
    class _Participant:
        @try_method()
        async def do_try(self, ctx: TccContext) -> str:
            return f"try-{pid}"

        @confirm_method()
        async def do_confirm(self, ctx: TccContext) -> None:
            pass

        @cancel_method()
        async def do_cancel(self, ctx: TccContext) -> None:
            pass

    return ParticipantDefinition(
        id=pid,
        order=order,
        timeout_ms=timeout_ms,
        optional=optional,
        participant_class=_Participant,
        try_method=_Participant.do_try if True else None,
        confirm_method=_Participant.do_confirm if has_confirm else None,
        cancel_method=_Participant.do_cancel if has_cancel else None,
    )


def _make_tcc_def(
    name: str = "test-tcc",
    participants: list[ParticipantDefinition] | None = None,
) -> TccDefinition:
    """Create a minimal TccDefinition."""
    participants = participants or [
        _make_participant_def("p1", order=1),
        _make_participant_def("p2", order=2),
    ]
    return TccDefinition(
        name=name,
        bean=_FakeBean(),
        participants={p.id: p for p in participants},
    )


# ── TccArgumentResolver ─────────────────────────────────────


class TestTccArgumentResolver:
    """Tests for TccArgumentResolver."""

    def test_resolve_tcc_context_by_type(self) -> None:
        """TccContext parameter is resolved by type."""
        resolver = TccArgumentResolver()
        ctx = TccContext(tcc_name="test")

        async def method(self: Any, ctx: TccContext) -> None:
            pass

        result = resolver.resolve(method, _FakeBean(), ctx, input_data=None)
        assert result["ctx"] is ctx

    def test_resolve_input_whole(self) -> None:
        """Annotated[T, Input()] injects the full input data."""
        resolver = TccArgumentResolver()
        ctx = TccContext(tcc_name="test")
        data = {"order_id": 123}

        async def method(self: Any, data: Annotated[dict, Input()]) -> None:
            pass

        result = resolver.resolve(method, _FakeBean(), ctx, input_data=data)
        assert result["data"] == {"order_id": 123}

    def test_resolve_input_with_key(self) -> None:
        """Annotated[T, Input("key")] injects a specific key from input data."""
        resolver = TccArgumentResolver()
        ctx = TccContext(tcc_name="test")
        data = {"order_id": 123, "amount": 99.99}

        async def method(self: Any, order_id: Annotated[int, Input("order_id")]) -> None:
            pass

        result = resolver.resolve(method, _FakeBean(), ctx, input_data=data)
        assert result["order_id"] == 123

    def test_resolve_from_try(self) -> None:
        """Annotated[T, FromTry()] injects the try result for this participant."""
        resolver = TccArgumentResolver()
        ctx = TccContext(tcc_name="test")
        ctx.set_try_result("p1", "reservation-abc")

        async def method(self: Any, reservation: Annotated[str, FromTry()]) -> None:
            pass

        result = resolver.resolve(method, _FakeBean(), ctx, input_data=None, participant_id="p1")
        assert result["reservation"] == "reservation-abc"

    def test_skip_self(self) -> None:
        """Parameter named 'self' is skipped."""
        resolver = TccArgumentResolver()
        ctx = TccContext(tcc_name="test")

        async def method(self: Any, ctx: TccContext) -> None:
            pass

        result = resolver.resolve(method, _FakeBean(), ctx, input_data=None)
        assert "self" not in result
        assert "ctx" in result

    def test_unresolvable_raises_type_error(self) -> None:
        """Unresolvable parameter raises TypeError."""
        resolver = TccArgumentResolver()
        ctx = TccContext(tcc_name="test")

        async def method(self: Any, unknown: str) -> None:
            pass

        with pytest.raises(TypeError, match="Cannot resolve"):
            resolver.resolve(method, _FakeBean(), ctx, input_data=None)


# ── TccParticipantInvoker ────────────────────────────────────


class TestTccParticipantInvoker:
    """Tests for TccParticipantInvoker."""

    @pytest.mark.anyio
    async def test_invoke_try(self) -> None:
        """invoke_try calls try_method with resolved arguments and returns result."""
        resolver = TccArgumentResolver()
        invoker = TccParticipantInvoker(resolver)
        ctx = TccContext(tcc_name="test")

        async def my_try(self: Any, ctx: TccContext) -> str:
            return "reservation-123"

        p_def = ParticipantDefinition(
            id="p1",
            order=1,
            try_method=my_try,
        )

        result = await invoker.invoke_try(p_def, _FakeBean(), ctx, input_data=None)
        assert result == "reservation-123"

    @pytest.mark.anyio
    async def test_invoke_confirm(self) -> None:
        """invoke_confirm calls confirm_method with resolved arguments."""
        resolver = TccArgumentResolver()
        invoker = TccParticipantInvoker(resolver)
        ctx = TccContext(tcc_name="test")
        ctx.set_try_result("p1", "reservation-123")

        confirmed = False

        async def my_confirm(
            self: Any,
            reservation: Annotated[str, FromTry()],
        ) -> None:
            nonlocal confirmed
            assert reservation == "reservation-123"
            confirmed = True

        p_def = ParticipantDefinition(
            id="p1",
            order=1,
            try_method=MagicMock(),
            confirm_method=my_confirm,
        )

        await invoker.invoke_confirm(p_def, _FakeBean(), ctx)
        assert confirmed

    @pytest.mark.anyio
    async def test_invoke_cancel(self) -> None:
        """invoke_cancel calls cancel_method with resolved arguments."""
        resolver = TccArgumentResolver()
        invoker = TccParticipantInvoker(resolver)
        ctx = TccContext(tcc_name="test")
        ctx.set_try_result("p1", "reservation-123")

        cancelled = False

        async def my_cancel(
            self: Any,
            reservation: Annotated[str, FromTry()],
        ) -> None:
            nonlocal cancelled
            assert reservation == "reservation-123"
            cancelled = True

        p_def = ParticipantDefinition(
            id="p1",
            order=1,
            try_method=MagicMock(),
            cancel_method=my_cancel,
        )

        await invoker.invoke_cancel(p_def, _FakeBean(), ctx)
        assert cancelled

    @pytest.mark.anyio
    async def test_invoke_try_no_method_raises(self) -> None:
        """invoke_try with no try_method raises ValueError."""
        resolver = TccArgumentResolver()
        invoker = TccParticipantInvoker(resolver)
        ctx = TccContext(tcc_name="test")

        p_def = ParticipantDefinition(
            id="p1",
            order=1,
            try_method=None,
        )

        with pytest.raises(ValueError, match="no try_method"):
            await invoker.invoke_try(p_def, _FakeBean(), ctx, input_data=None)

    @pytest.mark.anyio
    async def test_invoke_confirm_no_method_raises(self) -> None:
        """invoke_confirm with no confirm_method raises ValueError."""
        resolver = TccArgumentResolver()
        invoker = TccParticipantInvoker(resolver)
        ctx = TccContext(tcc_name="test")

        p_def = ParticipantDefinition(
            id="p1",
            order=1,
            try_method=MagicMock(),
            confirm_method=None,
        )

        with pytest.raises(ValueError, match="no confirm_method"):
            await invoker.invoke_confirm(p_def, _FakeBean(), ctx)

    @pytest.mark.anyio
    async def test_invoke_cancel_no_method_raises(self) -> None:
        """invoke_cancel with no cancel_method raises ValueError."""
        resolver = TccArgumentResolver()
        invoker = TccParticipantInvoker(resolver)
        ctx = TccContext(tcc_name="test")

        p_def = ParticipantDefinition(
            id="p1",
            order=1,
            try_method=MagicMock(),
            cancel_method=None,
        )

        with pytest.raises(ValueError, match="no cancel_method"):
            await invoker.invoke_cancel(p_def, _FakeBean(), ctx)

    @pytest.mark.anyio
    async def test_invoke_sync_try(self) -> None:
        """invoke_try works with synchronous try_method."""
        resolver = TccArgumentResolver()
        invoker = TccParticipantInvoker(resolver)
        ctx = TccContext(tcc_name="test")

        def my_sync_try(self: Any, ctx: TccContext) -> str:
            return "sync-result"

        p_def = ParticipantDefinition(
            id="p1",
            order=1,
            try_method=my_sync_try,
        )

        result = await invoker.invoke_try(p_def, _FakeBean(), ctx, input_data=None)
        assert result == "sync-result"


# ── TccExecutionOrchestrator ─────────────────────────────────


class TestTccExecutionOrchestrator:
    """Tests for TccExecutionOrchestrator three-phase execution."""

    @pytest.mark.anyio
    async def test_all_try_succeed_then_confirm_all(self) -> None:
        """When all TRY succeed, CONFIRM is called for all participants."""
        invoker = AsyncMock(spec=TccParticipantInvoker)
        invoker.invoke_try = AsyncMock(side_effect=["try-p1", "try-p2"])
        invoker.invoke_confirm = AsyncMock(return_value=None)
        invoker.invoke_cancel = AsyncMock(return_value=None)

        orchestrator = TccExecutionOrchestrator(invoker)
        tcc_def = _make_tcc_def()
        ctx = TccContext(tcc_name="test-tcc")

        success, failed_id = await orchestrator.execute(tcc_def, ctx, input_data=None)

        assert success is True
        assert failed_id is None
        assert invoker.invoke_try.call_count == 2
        assert invoker.invoke_confirm.call_count == 2
        assert invoker.invoke_cancel.call_count == 0

    @pytest.mark.anyio
    async def test_try_fails_on_second_participant_cancels_first(self) -> None:
        """When TRY fails on p2, CANCEL is called for p1 (which succeeded TRY)."""
        invoker = AsyncMock(spec=TccParticipantInvoker)
        invoker.invoke_try = AsyncMock(side_effect=["try-p1", RuntimeError("p2 try failed")])
        invoker.invoke_confirm = AsyncMock(return_value=None)
        invoker.invoke_cancel = AsyncMock(return_value=None)

        orchestrator = TccExecutionOrchestrator(invoker)
        tcc_def = _make_tcc_def()
        ctx = TccContext(tcc_name="test-tcc")

        success, failed_id = await orchestrator.execute(tcc_def, ctx, input_data=None)

        assert success is False
        assert failed_id == "p2"
        assert invoker.invoke_try.call_count == 2
        assert invoker.invoke_confirm.call_count == 0
        # Only p1 completed TRY, so only p1 should be cancelled
        assert invoker.invoke_cancel.call_count == 1

    @pytest.mark.anyio
    async def test_context_try_results_populated(self) -> None:
        """TccContext.try_results is populated during TRY phase."""
        invoker = AsyncMock(spec=TccParticipantInvoker)
        invoker.invoke_try = AsyncMock(side_effect=["try-p1", "try-p2"])
        invoker.invoke_confirm = AsyncMock(return_value=None)

        orchestrator = TccExecutionOrchestrator(invoker)
        tcc_def = _make_tcc_def()
        ctx = TccContext(tcc_name="test-tcc")

        await orchestrator.execute(tcc_def, ctx, input_data=None)

        assert ctx.try_results["p1"] == "try-p1"
        assert ctx.try_results["p2"] == "try-p2"

    @pytest.mark.anyio
    async def test_confirm_fails_triggers_cancel(self) -> None:
        """When CONFIRM fails, CANCEL is called for all participants that completed TRY."""
        invoker = AsyncMock(spec=TccParticipantInvoker)
        invoker.invoke_try = AsyncMock(side_effect=["try-p1", "try-p2"])
        invoker.invoke_confirm = AsyncMock(side_effect=[None, RuntimeError("p2 confirm failed")])
        invoker.invoke_cancel = AsyncMock(return_value=None)

        orchestrator = TccExecutionOrchestrator(invoker)
        tcc_def = _make_tcc_def()
        ctx = TccContext(tcc_name="test-tcc")

        success, failed_id = await orchestrator.execute(tcc_def, ctx, input_data=None)

        assert success is False
        assert failed_id == "p2"
        # Both should be cancelled since both completed TRY
        assert invoker.invoke_cancel.call_count == 2

    @pytest.mark.anyio
    async def test_optional_participant_try_failure_skipped(self) -> None:
        """Optional participant's TRY failure is skipped; execution continues."""
        invoker = AsyncMock(spec=TccParticipantInvoker)
        invoker.invoke_try = AsyncMock(side_effect=[RuntimeError("p1 optional fail"), "try-p2"])
        invoker.invoke_confirm = AsyncMock(return_value=None)
        invoker.invoke_cancel = AsyncMock(return_value=None)

        orchestrator = TccExecutionOrchestrator(invoker)
        p1 = _make_participant_def("p1", order=1, optional=True)
        p2 = _make_participant_def("p2", order=2)
        tcc_def = _make_tcc_def(participants=[p1, p2])
        ctx = TccContext(tcc_name="test-tcc")

        success, failed_id = await orchestrator.execute(tcc_def, ctx, input_data=None)

        assert success is True
        assert failed_id is None
        # Only p2 should be confirmed (p1 was optional and failed)
        assert invoker.invoke_confirm.call_count == 1

    @pytest.mark.anyio
    async def test_retry_on_try_failure(self) -> None:
        """TRY phase retries on failure up to the configured retry count."""
        call_count = 0

        async def _try_with_retry(*args: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            return "try-p1"

        invoker = AsyncMock(spec=TccParticipantInvoker)
        invoker.invoke_try = AsyncMock(side_effect=_try_with_retry)
        invoker.invoke_confirm = AsyncMock(return_value=None)

        orchestrator = TccExecutionOrchestrator(invoker)

        # Create participant with retry
        p1_def = _make_participant_def("p1", order=1)
        # Override the try_method retry metadata
        p1_def = ParticipantDefinition(
            id="p1",
            order=1,
            participant_class=p1_def.participant_class,
            try_method=p1_def.try_method,
            confirm_method=p1_def.confirm_method,
            cancel_method=p1_def.cancel_method,
        )

        tcc_def = _make_tcc_def(participants=[p1_def])
        # Set retry at the try_method level
        if p1_def.try_method is not None:
            p1_def.try_method.__pyfly_try_method__ = {"timeout_ms": 0, "retry": 2, "backoff_ms": 0}

        ctx = TccContext(tcc_name="test-tcc")
        success, failed_id = await orchestrator.execute(tcc_def, ctx, input_data=None)

        assert success is True
        assert call_count == 2


# ── TccEngine ────────────────────────────────────────────────


@pytest.fixture
def mock_registry() -> MagicMock:
    return MagicMock(spec=TccRegistry)


@pytest.fixture
def mock_invoker() -> AsyncMock:
    return AsyncMock(spec=TccParticipantInvoker)


@pytest.fixture
def mock_orchestrator() -> AsyncMock:
    mock = AsyncMock(spec=TccExecutionOrchestrator)
    mock.execute = AsyncMock(return_value=(True, None))
    return mock


@pytest.fixture
def mock_persistence() -> AsyncMock:
    mock = AsyncMock()
    mock.persist_state = AsyncMock(return_value=None)
    mock.mark_completed = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def mock_events() -> AsyncMock:
    mock = AsyncMock()
    mock.on_start = AsyncMock(return_value=None)
    mock.on_completed = AsyncMock(return_value=None)
    return mock


def _build_engine(
    registry: MagicMock,
    invoker: AsyncMock,
    orchestrator: AsyncMock,
    persistence: AsyncMock | None = None,
    events: AsyncMock | None = None,
) -> TccEngine:
    return TccEngine(
        registry=registry,
        participant_invoker=invoker,
        orchestrator=orchestrator,
        persistence_port=persistence,
        events_port=events,
    )


class TestTccEngineSuccessful:
    """Tests for successful TCC execution."""

    @pytest.mark.anyio
    async def test_all_try_succeed_confirm_all(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
        mock_events: AsyncMock,
        mock_persistence: AsyncMock,
    ) -> None:
        """Successful TCC → TccResult.success is True with populated try_results."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def

        async def _exec_side_effect(tcc_def: Any, ctx: TccContext, input_data: Any = None) -> tuple[bool, str | None]:
            ctx.set_try_result("p1", "try-p1")
            ctx.set_try_result("p2", "try-p2")
            ctx.set_participant_status("p1", TccPhase.CONFIRM)
            ctx.set_participant_status("p2", TccPhase.CONFIRM)
            ctx.set_phase(TccPhase.CONFIRM)
            return (True, None)

        mock_orchestrator.execute = AsyncMock(side_effect=_exec_side_effect)

        engine = _build_engine(
            mock_registry,
            mock_invoker,
            mock_orchestrator,
            mock_persistence,
            mock_events,
        )

        result = await engine.execute("test-tcc", input_data={"order": 1})

        assert isinstance(result, TccResult)
        assert result.success is True
        assert result.error is None
        assert result.tcc_name == "test-tcc"
        assert result.try_results["p1"] == "try-p1"
        assert result.try_results["p2"] == "try-p2"
        assert result.final_phase == TccPhase.CONFIRM
        assert "p1" in result.participant_results
        assert "p2" in result.participant_results

    @pytest.mark.anyio
    async def test_tcc_result_populated_correctly(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
    ) -> None:
        """TccResult fields populated correctly: try_results, participant_results, final_phase."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def

        async def _exec_side_effect(tcc_def: Any, ctx: TccContext, input_data: Any = None) -> tuple[bool, str | None]:
            ctx.set_try_result("p1", "reservation-1")
            ctx.set_try_result("p2", "reservation-2")
            ctx.set_participant_status("p1", TccPhase.CONFIRM)
            ctx.set_participant_status("p2", TccPhase.CONFIRM)
            ctx.set_phase(TccPhase.CONFIRM)
            return (True, None)

        mock_orchestrator.execute = AsyncMock(side_effect=_exec_side_effect)

        engine = _build_engine(mock_registry, mock_invoker, mock_orchestrator)
        result = await engine.execute("test-tcc")

        assert result.correlation_id is not None
        assert len(result.correlation_id) > 0
        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.started_at <= result.completed_at
        assert result.failed_participant_id is None


class TestTccEngineFailure:
    """Tests for failed TCC execution."""

    @pytest.mark.anyio
    async def test_try_fails_returns_failure_result(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
    ) -> None:
        """TRY failure → TccResult.success is False, failed_participant_id set."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def

        async def _exec_side_effect(tcc_def: Any, ctx: TccContext, input_data: Any = None) -> tuple[bool, str | None]:
            ctx.set_try_result("p1", "try-p1")
            ctx.set_participant_status("p1", TccPhase.CANCEL)
            ctx.set_phase(TccPhase.CANCEL)
            return (False, "p2")

        mock_orchestrator.execute = AsyncMock(side_effect=_exec_side_effect)

        engine = _build_engine(mock_registry, mock_invoker, mock_orchestrator)
        result = await engine.execute("test-tcc")

        assert result.success is False
        assert result.failed_participant_id == "p2"
        assert result.final_phase == TccPhase.CANCEL


class TestTccEngineUnknown:
    """Tests for unknown TCC name."""

    @pytest.mark.anyio
    async def test_unknown_tcc_raises_value_error(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
    ) -> None:
        """Unknown TCC name raises ValueError."""
        mock_registry.get.return_value = None

        engine = _build_engine(mock_registry, mock_invoker, mock_orchestrator)

        with pytest.raises(ValueError, match="not registered"):
            await engine.execute("non-existent-tcc")


class TestTccEngineEvents:
    """Tests for event emission."""

    @pytest.mark.anyio
    async def test_events_emitted_on_success(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
        mock_events: AsyncMock,
    ) -> None:
        """Events port receives on_start and on_completed on success."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(True, None))

        engine = _build_engine(
            mock_registry,
            mock_invoker,
            mock_orchestrator,
            events=mock_events,
        )

        result = await engine.execute("test-tcc")

        mock_events.on_start.assert_called_once_with(
            "test-tcc",
            result.correlation_id,
        )
        mock_events.on_completed.assert_called_once_with(
            "test-tcc",
            result.correlation_id,
            True,
        )

    @pytest.mark.anyio
    async def test_events_emitted_on_failure(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
        mock_events: AsyncMock,
    ) -> None:
        """Events port receives on_completed(success=False) on failure."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(False, "p2"))

        engine = _build_engine(
            mock_registry,
            mock_invoker,
            mock_orchestrator,
            events=mock_events,
        )

        result = await engine.execute("test-tcc")

        mock_events.on_start.assert_called_once()
        mock_events.on_completed.assert_called_once_with(
            "test-tcc",
            result.correlation_id,
            False,
        )

    @pytest.mark.anyio
    async def test_no_events_port_configured(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
    ) -> None:
        """Engine works without events port configured."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(True, None))

        engine = _build_engine(
            mock_registry,
            mock_invoker,
            mock_orchestrator,
            events=None,
        )

        result = await engine.execute("test-tcc")
        assert result.success is True


class TestTccEnginePersistence:
    """Tests for persistence port integration."""

    @pytest.mark.anyio
    async def test_persistence_called_on_success(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
        mock_persistence: AsyncMock,
    ) -> None:
        """Persistence port receives persist_state and mark_completed on success."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(True, None))

        engine = _build_engine(
            mock_registry,
            mock_invoker,
            mock_orchestrator,
            persistence=mock_persistence,
        )

        result = await engine.execute("test-tcc")

        mock_persistence.persist_state.assert_called_once()
        state = mock_persistence.persist_state.call_args[0][0]
        assert state["tcc_name"] == "test-tcc"
        assert state["correlation_id"] == result.correlation_id

        mock_persistence.mark_completed.assert_called_once_with(
            result.correlation_id,
            True,
        )

    @pytest.mark.anyio
    async def test_persistence_called_on_failure(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
        mock_persistence: AsyncMock,
    ) -> None:
        """Persistence port receives mark_completed(False) on failure."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(False, "p1"))

        engine = _build_engine(
            mock_registry,
            mock_invoker,
            mock_orchestrator,
            persistence=mock_persistence,
        )

        result = await engine.execute("test-tcc")

        mock_persistence.persist_state.assert_called_once()
        mock_persistence.mark_completed.assert_called_once_with(
            result.correlation_id,
            False,
        )

    @pytest.mark.anyio
    async def test_no_persistence_port_configured(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
    ) -> None:
        """Engine works without persistence port configured."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(True, None))

        engine = _build_engine(
            mock_registry,
            mock_invoker,
            mock_orchestrator,
            persistence=None,
        )

        result = await engine.execute("test-tcc")
        assert result.success is True


class TestTccEngineCorrelationId:
    """Tests for correlation ID handling."""

    @pytest.mark.anyio
    async def test_custom_correlation_id(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
    ) -> None:
        """Custom correlation_id is propagated to the result."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(True, None))

        engine = _build_engine(mock_registry, mock_invoker, mock_orchestrator)
        result = await engine.execute(
            "test-tcc",
            correlation_id="custom-123",
        )

        assert result.correlation_id == "custom-123"

    @pytest.mark.anyio
    async def test_auto_generated_correlation_id(
        self,
        mock_registry: MagicMock,
        mock_invoker: AsyncMock,
        mock_orchestrator: AsyncMock,
    ) -> None:
        """Auto-generated correlation_id when not provided."""
        tcc_def = _make_tcc_def()
        mock_registry.get.return_value = tcc_def
        mock_orchestrator.execute = AsyncMock(return_value=(True, None))

        engine = _build_engine(mock_registry, mock_invoker, mock_orchestrator)
        result = await engine.execute("test-tcc")

        assert result.correlation_id is not None
        assert len(result.correlation_id) > 0
