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
"""Tests for TCC annotations — decorators and parameter injection markers."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses

from pyfly.transactional.tcc.annotations import (
    FromTry,
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)

# ---------------------------------------------------------------------------
# Decorated fixtures
# ---------------------------------------------------------------------------


@tcc(name="order-payment", timeout_ms=30000, retry_enabled=True, max_retries=3, backoff_ms=1000)
class OrderPaymentTcc:
    @tcc_participant(id="payment-service", order=1, timeout_ms=5000, optional=False)
    class PaymentParticipant:
        @try_method(timeout_ms=5000, retry=2, backoff_ms=100)
        async def try_reserve(self, request: str, ctx: object) -> str:
            return "reserved"

        @confirm_method(timeout_ms=10000, retry=3)
        async def confirm(self, reservation_id: str, ctx: object) -> None:
            pass

        @cancel_method(timeout_ms=5000, retry=1)
        async def cancel(self, reservation_id: str) -> None:
            pass


@tcc(name="default-tcc")
class DefaultTcc:
    pass


@tcc_participant(id="inventory")
class DefaultParticipant:
    pass


# Standalone sync functions for method decorator tests


@try_method()
def plain_try(self) -> str:
    return "tried"


@confirm_method()
def plain_confirm(self) -> None:
    pass


@cancel_method()
def plain_cancel(self) -> None:
    pass


# ---------------------------------------------------------------------------
# Tests — @tcc class decorator
# ---------------------------------------------------------------------------


class TestTccDecorator:
    def test_sets_metadata_dict(self) -> None:
        meta = OrderPaymentTcc.__pyfly_tcc__
        assert isinstance(meta, dict)

    def test_name(self) -> None:
        assert OrderPaymentTcc.__pyfly_tcc__["name"] == "order-payment"

    def test_timeout_ms(self) -> None:
        assert OrderPaymentTcc.__pyfly_tcc__["timeout_ms"] == 30000

    def test_retry_enabled(self) -> None:
        assert OrderPaymentTcc.__pyfly_tcc__["retry_enabled"] is True

    def test_max_retries(self) -> None:
        assert OrderPaymentTcc.__pyfly_tcc__["max_retries"] == 3

    def test_backoff_ms(self) -> None:
        assert OrderPaymentTcc.__pyfly_tcc__["backoff_ms"] == 1000

    def test_returns_same_class(self) -> None:
        assert OrderPaymentTcc.__name__ == "OrderPaymentTcc"

    def test_does_not_alter_bases(self) -> None:
        assert OrderPaymentTcc.__bases__ == (object,)

    def test_all_keys_present(self) -> None:
        expected_keys = {"name", "timeout_ms", "retry_enabled", "max_retries", "backoff_ms"}
        assert set(OrderPaymentTcc.__pyfly_tcc__.keys()) == expected_keys


class TestTccDefaults:
    def test_default_name(self) -> None:
        assert DefaultTcc.__pyfly_tcc__["name"] == "default-tcc"

    def test_default_timeout_ms(self) -> None:
        assert DefaultTcc.__pyfly_tcc__["timeout_ms"] == 0

    def test_default_retry_enabled(self) -> None:
        assert DefaultTcc.__pyfly_tcc__["retry_enabled"] is False

    def test_default_max_retries(self) -> None:
        assert DefaultTcc.__pyfly_tcc__["max_retries"] == 0

    def test_default_backoff_ms(self) -> None:
        assert DefaultTcc.__pyfly_tcc__["backoff_ms"] == 0


# ---------------------------------------------------------------------------
# Tests — @tcc_participant class decorator
# ---------------------------------------------------------------------------


class TestTccParticipantDecorator:
    def test_sets_metadata_dict(self) -> None:
        meta = OrderPaymentTcc.PaymentParticipant.__pyfly_tcc_participant__
        assert isinstance(meta, dict)

    def test_id(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.__pyfly_tcc_participant__["id"] == "payment-service"

    def test_order(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.__pyfly_tcc_participant__["order"] == 1

    def test_timeout_ms(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.__pyfly_tcc_participant__["timeout_ms"] == 5000

    def test_optional(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.__pyfly_tcc_participant__["optional"] is False

    def test_all_keys_present(self) -> None:
        expected_keys = {"id", "order", "timeout_ms", "optional"}
        assert set(OrderPaymentTcc.PaymentParticipant.__pyfly_tcc_participant__.keys()) == expected_keys

    def test_returns_same_class(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.__name__ == "PaymentParticipant"


class TestTccParticipantDefaults:
    def test_default_id(self) -> None:
        assert DefaultParticipant.__pyfly_tcc_participant__["id"] == "inventory"

    def test_default_order(self) -> None:
        assert DefaultParticipant.__pyfly_tcc_participant__["order"] == 0

    def test_default_timeout_ms(self) -> None:
        assert DefaultParticipant.__pyfly_tcc_participant__["timeout_ms"] == 0

    def test_default_optional(self) -> None:
        assert DefaultParticipant.__pyfly_tcc_participant__["optional"] is False


# ---------------------------------------------------------------------------
# Tests — @try_method decorator
# ---------------------------------------------------------------------------


class TestTryMethodDecorator:
    def test_sets_metadata_dict(self) -> None:
        meta = OrderPaymentTcc.PaymentParticipant.try_reserve.__pyfly_try_method__
        assert isinstance(meta, dict)

    def test_timeout_ms(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.try_reserve.__pyfly_try_method__["timeout_ms"] == 5000

    def test_retry(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.try_reserve.__pyfly_try_method__["retry"] == 2

    def test_backoff_ms(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.try_reserve.__pyfly_try_method__["backoff_ms"] == 100

    def test_all_keys_present(self) -> None:
        expected_keys = {"timeout_ms", "retry", "backoff_ms"}
        assert set(OrderPaymentTcc.PaymentParticipant.try_reserve.__pyfly_try_method__.keys()) == expected_keys

    def test_preserves_function_name(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.try_reserve.__name__ == "try_reserve"

    def test_preserves_function_qualname(self) -> None:
        assert "try_reserve" in OrderPaymentTcc.PaymentParticipant.try_reserve.__qualname__

    def test_works_with_async_function(self) -> None:
        participant = OrderPaymentTcc.PaymentParticipant()
        result = asyncio.run(participant.try_reserve("req", object()))
        assert result == "reserved"

    def test_defaults(self) -> None:
        meta = plain_try.__pyfly_try_method__
        assert meta["timeout_ms"] == 0
        assert meta["retry"] == 0
        assert meta["backoff_ms"] == 0

    def test_preserves_sync_function(self) -> None:
        assert plain_try(None) == "tried"


# ---------------------------------------------------------------------------
# Tests — @confirm_method decorator
# ---------------------------------------------------------------------------


class TestConfirmMethodDecorator:
    def test_sets_metadata_dict(self) -> None:
        meta = OrderPaymentTcc.PaymentParticipant.confirm.__pyfly_confirm_method__
        assert isinstance(meta, dict)

    def test_timeout_ms(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.confirm.__pyfly_confirm_method__["timeout_ms"] == 10000

    def test_retry(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.confirm.__pyfly_confirm_method__["retry"] == 3

    def test_backoff_ms(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.confirm.__pyfly_confirm_method__["backoff_ms"] == 0

    def test_all_keys_present(self) -> None:
        expected_keys = {"timeout_ms", "retry", "backoff_ms"}
        assert set(OrderPaymentTcc.PaymentParticipant.confirm.__pyfly_confirm_method__.keys()) == expected_keys

    def test_preserves_function_name(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.confirm.__name__ == "confirm"

    def test_works_with_async_function(self) -> None:
        participant = OrderPaymentTcc.PaymentParticipant()
        result = asyncio.run(participant.confirm("res-123", object()))
        assert result is None

    def test_defaults(self) -> None:
        meta = plain_confirm.__pyfly_confirm_method__
        assert meta["timeout_ms"] == 0
        assert meta["retry"] == 0
        assert meta["backoff_ms"] == 0


# ---------------------------------------------------------------------------
# Tests — @cancel_method decorator
# ---------------------------------------------------------------------------


class TestCancelMethodDecorator:
    def test_sets_metadata_dict(self) -> None:
        meta = OrderPaymentTcc.PaymentParticipant.cancel.__pyfly_cancel_method__
        assert isinstance(meta, dict)

    def test_timeout_ms(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.cancel.__pyfly_cancel_method__["timeout_ms"] == 5000

    def test_retry(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.cancel.__pyfly_cancel_method__["retry"] == 1

    def test_backoff_ms(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.cancel.__pyfly_cancel_method__["backoff_ms"] == 0

    def test_all_keys_present(self) -> None:
        expected_keys = {"timeout_ms", "retry", "backoff_ms"}
        assert set(OrderPaymentTcc.PaymentParticipant.cancel.__pyfly_cancel_method__.keys()) == expected_keys

    def test_preserves_function_name(self) -> None:
        assert OrderPaymentTcc.PaymentParticipant.cancel.__name__ == "cancel"

    def test_works_with_async_function(self) -> None:
        participant = OrderPaymentTcc.PaymentParticipant()
        result = asyncio.run(participant.cancel("res-123"))
        assert result is None

    def test_defaults(self) -> None:
        meta = plain_cancel.__pyfly_cancel_method__
        assert meta["timeout_ms"] == 0
        assert meta["retry"] == 0
        assert meta["backoff_ms"] == 0


# ---------------------------------------------------------------------------
# Tests — FromTry marker
# ---------------------------------------------------------------------------


class TestFromTryMarker:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(FromTry)
        marker = FromTry()
        with contextlib.suppress(Exception):
            marker.__dict__["x"] = 1  # type: ignore[index]
        # Frozen dataclass — attribute assignment should raise
        with contextlib.suppress(dataclasses.FrozenInstanceError):
            object.__setattr__(marker, "x", 1)

    def test_no_fields(self) -> None:
        fields = dataclasses.fields(FromTry)
        assert len(fields) == 0

    def test_instantiation(self) -> None:
        marker = FromTry()
        assert isinstance(marker, FromTry)


# ---------------------------------------------------------------------------
# Integration test — full nested decorated structure
# ---------------------------------------------------------------------------


class TestFullIntegration:
    def test_tcc_metadata_on_outer_class(self) -> None:
        assert hasattr(OrderPaymentTcc, "__pyfly_tcc__")
        assert OrderPaymentTcc.__pyfly_tcc__["name"] == "order-payment"

    def test_participant_metadata_on_nested_class(self) -> None:
        assert hasattr(OrderPaymentTcc.PaymentParticipant, "__pyfly_tcc_participant__")
        assert OrderPaymentTcc.PaymentParticipant.__pyfly_tcc_participant__["id"] == "payment-service"

    def test_try_method_metadata_on_method(self) -> None:
        assert hasattr(OrderPaymentTcc.PaymentParticipant.try_reserve, "__pyfly_try_method__")

    def test_confirm_method_metadata_on_method(self) -> None:
        assert hasattr(OrderPaymentTcc.PaymentParticipant.confirm, "__pyfly_confirm_method__")

    def test_cancel_method_metadata_on_method(self) -> None:
        assert hasattr(OrderPaymentTcc.PaymentParticipant.cancel, "__pyfly_cancel_method__")

    def test_all_methods_are_callable(self) -> None:
        participant = OrderPaymentTcc.PaymentParticipant()
        assert callable(participant.try_reserve)
        assert callable(participant.confirm)
        assert callable(participant.cancel)

    def test_async_try_returns_result(self) -> None:
        participant = OrderPaymentTcc.PaymentParticipant()
        result = asyncio.run(participant.try_reserve("req", object()))
        assert result == "reserved"
