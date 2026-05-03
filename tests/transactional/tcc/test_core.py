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
"""Tests for TCC core types — TccPhase, TccContext, TccResult, ParticipantResult."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from pyfly.transactional.tcc.core import (
    ParticipantResult,
    TccContext,
    TccPhase,
    TccResult,
)

# ---------------------------------------------------------------------------
# TccPhase
# ---------------------------------------------------------------------------


class TestTccPhase:
    """Tests for the TccPhase enum."""

    def test_members_count(self) -> None:
        assert len(TccPhase) == 3

    def test_try_value(self) -> None:
        assert TccPhase.TRY == "TRY"
        assert TccPhase.TRY.value == "TRY"

    def test_confirm_value(self) -> None:
        assert TccPhase.CONFIRM == "CONFIRM"
        assert TccPhase.CONFIRM.value == "CONFIRM"

    def test_cancel_value(self) -> None:
        assert TccPhase.CANCEL == "CANCEL"
        assert TccPhase.CANCEL.value == "CANCEL"

    def test_is_str_subclass(self) -> None:
        assert isinstance(TccPhase.TRY, str)


# ---------------------------------------------------------------------------
# TccContext
# ---------------------------------------------------------------------------


class TestTccContext:
    """Tests for the mutable TccContext dataclass."""

    def test_defaults(self) -> None:
        ctx = TccContext()
        assert ctx.correlation_id  # non-empty UUID string
        assert ctx.tcc_name == ""
        assert ctx.headers == {}
        assert ctx.variables == {}
        assert ctx.try_results == {}
        assert ctx.current_phase == TccPhase.TRY
        assert ctx.participant_statuses == {}

    def test_unique_correlation_ids(self) -> None:
        ctx1 = TccContext()
        ctx2 = TccContext()
        assert ctx1.correlation_id != ctx2.correlation_id

    def test_get_try_result_missing(self) -> None:
        ctx = TccContext()
        assert ctx.get_try_result("unknown") is None

    def test_set_and_get_try_result(self) -> None:
        ctx = TccContext()
        ctx.set_try_result("payment", {"reservation": "abc"})
        assert ctx.get_try_result("payment") == {"reservation": "abc"}

    def test_set_try_result_overwrites(self) -> None:
        ctx = TccContext()
        ctx.set_try_result("payment", "first")
        ctx.set_try_result("payment", "second")
        assert ctx.get_try_result("payment") == "second"

    def test_set_phase(self) -> None:
        ctx = TccContext()
        assert ctx.current_phase == TccPhase.TRY
        ctx.set_phase(TccPhase.CONFIRM)
        assert ctx.current_phase == TccPhase.CONFIRM

    def test_set_participant_status(self) -> None:
        ctx = TccContext()
        ctx.set_participant_status("inventory", TccPhase.TRY)
        ctx.set_participant_status("payment", TccPhase.CONFIRM)
        assert ctx.participant_statuses == {
            "inventory": TccPhase.TRY,
            "payment": TccPhase.CONFIRM,
        }

    def test_custom_fields(self) -> None:
        ctx = TccContext(
            tcc_name="order-payment",
            headers={"x-trace": "abc"},
            variables={"amount": 100},
        )
        assert ctx.tcc_name == "order-payment"
        assert ctx.headers == {"x-trace": "abc"}
        assert ctx.variables == {"amount": 100}


# ---------------------------------------------------------------------------
# ParticipantResult
# ---------------------------------------------------------------------------


class TestParticipantResult:
    """Tests for the frozen ParticipantResult dataclass."""

    def _make(self, **overrides) -> ParticipantResult:  # noqa: ANN003
        defaults = {
            "participant_id": "payment",
            "try_result": "reserved",
            "try_error": None,
            "confirm_error": None,
            "cancel_error": None,
            "final_phase": TccPhase.CONFIRM,
            "latency_ms": 42.5,
        }
        defaults.update(overrides)
        return ParticipantResult(**defaults)

    def test_frozen(self) -> None:
        pr = self._make()
        with pytest.raises(dataclasses.FrozenInstanceError):
            pr.participant_id = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        pr = self._make()
        assert pr.participant_id == "payment"
        assert pr.try_result == "reserved"
        assert pr.try_error is None
        assert pr.confirm_error is None
        assert pr.cancel_error is None
        assert pr.final_phase == TccPhase.CONFIRM
        assert pr.latency_ms == 42.5

    def test_with_errors(self) -> None:
        err = RuntimeError("try failed")
        pr = self._make(try_error=err, final_phase=TccPhase.CANCEL)
        assert pr.try_error is err
        assert pr.final_phase == TccPhase.CANCEL


# ---------------------------------------------------------------------------
# TccResult
# ---------------------------------------------------------------------------


class TestTccResult:
    """Tests for the frozen TccResult dataclass."""

    def _make_participant(
        self,
        participant_id: str = "payment",
        *,
        try_result: object = "reserved",
        try_error: Exception | None = None,
        confirm_error: Exception | None = None,
        cancel_error: Exception | None = None,
        final_phase: TccPhase = TccPhase.CONFIRM,
        latency_ms: float = 10.0,
    ) -> ParticipantResult:
        return ParticipantResult(
            participant_id=participant_id,
            try_result=try_result,
            try_error=try_error,
            confirm_error=confirm_error,
            cancel_error=cancel_error,
            final_phase=final_phase,
            latency_ms=latency_ms,
        )

    def _make_result(self, **overrides) -> TccResult:  # noqa: ANN003
        now = datetime.now(tz=UTC)
        pr = self._make_participant()
        defaults = {
            "correlation_id": "corr-1",
            "tcc_name": "order-payment",
            "success": True,
            "final_phase": TccPhase.CONFIRM,
            "try_results": {"payment": "reserved"},
            "participant_results": {"payment": pr},
            "started_at": now,
            "completed_at": now,
            "error": None,
            "failed_participant_id": None,
        }
        defaults.update(overrides)
        return TccResult(**defaults)

    def test_frozen(self) -> None:
        result = self._make_result()
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.success = False  # type: ignore[misc]

    def test_fields(self) -> None:
        result = self._make_result()
        assert result.correlation_id == "corr-1"
        assert result.tcc_name == "order-payment"
        assert result.success is True
        assert result.final_phase == TccPhase.CONFIRM
        assert result.error is None
        assert result.failed_participant_id is None

    def test_result_of_existing(self) -> None:
        result = self._make_result()
        assert result.result_of("payment") == "reserved"

    def test_result_of_missing(self) -> None:
        result = self._make_result()
        assert result.result_of("unknown") is None

    def test_failed_participants_empty(self) -> None:
        result = self._make_result()
        assert result.failed_participants() == {}

    def test_failed_participants_with_try_error(self) -> None:
        err = RuntimeError("boom")
        pr_ok = self._make_participant("inventory")
        pr_fail = self._make_participant("payment", try_error=err, final_phase=TccPhase.CANCEL)
        result = self._make_result(
            participant_results={"inventory": pr_ok, "payment": pr_fail},
            success=False,
        )
        failed = result.failed_participants()
        assert list(failed.keys()) == ["payment"]
        assert failed["payment"].try_error is err

    def test_failed_participants_with_confirm_error(self) -> None:
        err = RuntimeError("confirm failed")
        pr_fail = self._make_participant("payment", confirm_error=err)
        result = self._make_result(
            participant_results={"payment": pr_fail},
            success=False,
        )
        failed = result.failed_participants()
        assert "payment" in failed
        assert failed["payment"].confirm_error is err

    def test_failed_participants_with_cancel_error(self) -> None:
        err = RuntimeError("cancel failed")
        pr_fail = self._make_participant("payment", cancel_error=err, final_phase=TccPhase.CANCEL)
        result = self._make_result(
            participant_results={"payment": pr_fail},
            success=False,
        )
        failed = result.failed_participants()
        assert "payment" in failed
        assert failed["payment"].cancel_error is err
