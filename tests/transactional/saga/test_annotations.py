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
"""Tests for saga annotations — @saga, @saga_step, and parameter injection markers."""

from __future__ import annotations

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
    compensation_step,
    external_step,
    saga,
    saga_step,
    step_event,
)

# ---------------------------------------------------------------------------
# @saga
# ---------------------------------------------------------------------------


class TestSagaDecorator:
    def test_sets_pyfly_saga_metadata(self) -> None:
        @saga(name="order-saga")
        class OrderSaga:
            pass

        assert hasattr(OrderSaga, "__pyfly_saga__")
        assert OrderSaga.__pyfly_saga__["name"] == "order-saga"

    def test_sets_layer_concurrency(self) -> None:
        @saga(name="payment-saga", layer_concurrency=3)
        class PaymentSaga:
            pass

        assert PaymentSaga.__pyfly_saga__["layer_concurrency"] == 3

    def test_defaults_layer_concurrency_to_zero(self) -> None:
        @saga(name="shipping-saga")
        class ShippingSaga:
            pass

        assert ShippingSaga.__pyfly_saga__["layer_concurrency"] == 0

    def test_returns_same_class(self) -> None:
        class OriginalSaga:
            pass

        decorated = saga(name="test")(OriginalSaga)
        assert decorated is OriginalSaga

    def test_metadata_dict_has_exactly_name_and_layer_concurrency(self) -> None:
        @saga(name="my-saga")
        class MySaga:
            pass

        assert set(MySaga.__pyfly_saga__.keys()) == {"name", "layer_concurrency"}


# ---------------------------------------------------------------------------
# @saga_step
# ---------------------------------------------------------------------------


class TestSagaStepDecorator:
    def test_sets_pyfly_saga_step_metadata(self) -> None:
        @saga_step(id="reserve-inventory")
        async def reserve_inventory() -> str:
            return "reserved"

        assert hasattr(reserve_inventory, "__pyfly_saga_step__")
        assert reserve_inventory.__pyfly_saga_step__["id"] == "reserve-inventory"

    def test_defaults_compensate_to_none(self) -> None:
        @saga_step(id="step-a")
        async def step_a() -> None:
            pass

        assert reserve_inventory_meta(step_a)["compensate"] is None

    def test_defaults_depends_on_to_empty_list(self) -> None:
        @saga_step(id="step-b")
        async def step_b() -> None:
            pass

        assert reserve_inventory_meta(step_b)["depends_on"] == []

    def test_defaults_retry_to_zero(self) -> None:
        @saga_step(id="step-c")
        async def step_c() -> None:
            pass

        assert reserve_inventory_meta(step_c)["retry"] == 0

    def test_defaults_backoff_ms_to_zero(self) -> None:
        @saga_step(id="step-d")
        async def step_d() -> None:
            pass

        assert reserve_inventory_meta(step_d)["backoff_ms"] == 0

    def test_defaults_timeout_ms_to_zero(self) -> None:
        @saga_step(id="step-e")
        async def step_e() -> None:
            pass

        assert reserve_inventory_meta(step_e)["timeout_ms"] == 0

    def test_defaults_jitter_to_false(self) -> None:
        @saga_step(id="step-f")
        async def step_f() -> None:
            pass

        assert reserve_inventory_meta(step_f)["jitter"] is False

    def test_defaults_jitter_factor_to_zero(self) -> None:
        @saga_step(id="step-g")
        async def step_g() -> None:
            pass

        assert reserve_inventory_meta(step_g)["jitter_factor"] == 0.0

    def test_defaults_cpu_bound_to_false(self) -> None:
        @saga_step(id="step-h")
        async def step_h() -> None:
            pass

        assert reserve_inventory_meta(step_h)["cpu_bound"] is False

    def test_defaults_idempotency_key_to_none(self) -> None:
        @saga_step(id="step-i")
        async def step_i() -> None:
            pass

        assert reserve_inventory_meta(step_i)["idempotency_key"] is None

    def test_defaults_compensation_retry_to_none(self) -> None:
        @saga_step(id="step-j")
        async def step_j() -> None:
            pass

        assert reserve_inventory_meta(step_j)["compensation_retry"] is None

    def test_defaults_compensation_backoff_ms_to_none(self) -> None:
        @saga_step(id="step-k")
        async def step_k() -> None:
            pass

        assert reserve_inventory_meta(step_k)["compensation_backoff_ms"] is None

    def test_defaults_compensation_timeout_ms_to_none(self) -> None:
        @saga_step(id="step-l")
        async def step_l() -> None:
            pass

        assert reserve_inventory_meta(step_l)["compensation_timeout_ms"] is None

    def test_defaults_compensation_critical_to_false(self) -> None:
        @saga_step(id="step-m")
        async def step_m() -> None:
            pass

        assert reserve_inventory_meta(step_m)["compensation_critical"] is False

    def test_stores_compensate_when_provided(self) -> None:
        @saga_step(id="charge", compensate="refund")
        async def charge() -> None:
            pass

        assert reserve_inventory_meta(charge)["compensate"] == "refund"

    def test_stores_depends_on_when_provided(self) -> None:
        @saga_step(id="ship", depends_on=["charge", "reserve"])
        async def ship() -> None:
            pass

        assert reserve_inventory_meta(ship)["depends_on"] == ["charge", "reserve"]

    def test_stores_retry_and_backoff_ms(self) -> None:
        @saga_step(id="notify", retry=3, backoff_ms=500)
        async def notify() -> None:
            pass

        meta = reserve_inventory_meta(notify)
        assert meta["retry"] == 3
        assert meta["backoff_ms"] == 500

    def test_stores_jitter_options(self) -> None:
        @saga_step(id="jittered", jitter=True, jitter_factor=0.25)
        async def jittered() -> None:
            pass

        meta = reserve_inventory_meta(jittered)
        assert meta["jitter"] is True
        assert meta["jitter_factor"] == 0.25

    def test_stores_cpu_bound(self) -> None:
        @saga_step(id="compute", cpu_bound=True)
        async def compute() -> None:
            pass

        assert reserve_inventory_meta(compute)["cpu_bound"] is True

    def test_stores_idempotency_key(self) -> None:
        @saga_step(id="idem", idempotency_key="order-{id}")
        async def idem() -> None:
            pass

        assert reserve_inventory_meta(idem)["idempotency_key"] == "order-{id}"

    def test_stores_compensation_retry(self) -> None:
        @saga_step(id="comp-retry", compensation_retry=2)
        async def comp_retry() -> None:
            pass

        assert reserve_inventory_meta(comp_retry)["compensation_retry"] == 2

    def test_stores_compensation_critical(self) -> None:
        @saga_step(id="critical", compensation_critical=True)
        async def critical() -> None:
            pass

        assert reserve_inventory_meta(critical)["compensation_critical"] is True

    def test_preserves_function_name(self) -> None:
        @saga_step(id="named-step")
        async def my_function() -> str:
            return "hello"

        assert my_function.__name__ == "my_function"

    def test_preserves_function_docstring(self) -> None:
        @saga_step(id="docced-step")
        async def documented() -> str:
            """My docstring."""
            return "ok"

        assert documented.__doc__ == "My docstring."

    def test_preserves_function_behavior(self) -> None:
        import asyncio

        @saga_step(id="behave")
        async def add(a: int, b: int) -> int:
            return a + b

        result = asyncio.run(add(2, 3))
        assert result == 5

    def test_preserves_sync_function_behavior(self) -> None:
        @saga_step(id="sync-behave")
        def multiply(a: int, b: int) -> int:
            return a * b

        assert multiply(4, 5) == 20


# ---------------------------------------------------------------------------
# @step_event
# ---------------------------------------------------------------------------


class TestStepEventDecorator:
    def test_sets_pyfly_step_event_metadata(self) -> None:
        @step_event(topic="orders", event_type="OrderPlaced")
        async def handle_order() -> None:
            pass

        assert hasattr(handle_order, "__pyfly_step_event__")

    def test_stores_topic(self) -> None:
        @step_event(topic="payments", event_type="PaymentProcessed")
        async def handle_payment() -> None:
            pass

        assert handle_payment.__pyfly_step_event__["topic"] == "payments"

    def test_stores_event_type(self) -> None:
        @step_event(topic="payments", event_type="PaymentProcessed")
        async def handle_payment2() -> None:
            pass

        assert handle_payment2.__pyfly_step_event__["event_type"] == "PaymentProcessed"

    def test_defaults_key_to_none(self) -> None:
        @step_event(topic="inventory", event_type="StockReserved")
        async def handle_stock() -> None:
            pass

        assert handle_stock.__pyfly_step_event__["key"] is None

    def test_stores_key_when_provided(self) -> None:
        @step_event(topic="shipping", event_type="Shipped", key="order-id")
        async def handle_ship() -> None:
            pass

        assert handle_ship.__pyfly_step_event__["key"] == "order-id"

    def test_does_not_wrap_function(self) -> None:
        async def original() -> None:
            pass

        decorated = step_event(topic="t", event_type="E")(original)
        assert decorated is original


# ---------------------------------------------------------------------------
# @compensation_step
# ---------------------------------------------------------------------------


class TestCompensationStepDecorator:
    def test_sets_pyfly_compensation_step_metadata(self) -> None:
        @compensation_step(saga="order-saga", for_step_id="charge")
        class RefundStep:
            pass

        assert hasattr(RefundStep, "__pyfly_compensation_step__")

    def test_stores_saga_name(self) -> None:
        @compensation_step(saga="order-saga", for_step_id="charge")
        class RefundStep2:
            pass

        assert RefundStep2.__pyfly_compensation_step__["saga"] == "order-saga"

    def test_stores_for_step_id(self) -> None:
        @compensation_step(saga="order-saga", for_step_id="reserve")
        class ReleaseStep:
            pass

        assert ReleaseStep.__pyfly_compensation_step__["for_step_id"] == "reserve"

    def test_returns_same_class(self) -> None:
        class OriginalComp:
            pass

        decorated = compensation_step(saga="s", for_step_id="f")(OriginalComp)
        assert decorated is OriginalComp

    def test_metadata_dict_has_exactly_saga_and_for_step_id(self) -> None:
        @compensation_step(saga="s", for_step_id="f")
        class AComp:
            pass

        assert set(AComp.__pyfly_compensation_step__.keys()) == {"saga", "for_step_id"}


# ---------------------------------------------------------------------------
# @external_step
# ---------------------------------------------------------------------------


class TestExternalStepDecorator:
    def test_sets_pyfly_external_step_metadata(self) -> None:
        @external_step(saga="order-saga", id="payment-gateway")
        class PaymentGatewayStep:
            pass

        assert hasattr(PaymentGatewayStep, "__pyfly_external_step__")

    def test_stores_saga_name(self) -> None:
        @external_step(saga="order-saga", id="payment-gateway")
        class PG1:
            pass

        assert PG1.__pyfly_external_step__["saga"] == "order-saga"

    def test_stores_id(self) -> None:
        @external_step(saga="order-saga", id="payment-gateway")
        class PG2:
            pass

        assert PG2.__pyfly_external_step__["id"] == "payment-gateway"

    def test_defaults_compensate_to_none(self) -> None:
        @external_step(saga="s", id="i")
        class ES1:
            pass

        assert ES1.__pyfly_external_step__["compensate"] is None

    def test_defaults_depends_on_to_none(self) -> None:
        @external_step(saga="s", id="i")
        class ES2:
            pass

        assert ES2.__pyfly_external_step__["depends_on"] is None

    def test_defaults_retry_to_zero(self) -> None:
        @external_step(saga="s", id="i")
        class ES3:
            pass

        assert ES3.__pyfly_external_step__["retry"] == 0

    def test_defaults_backoff_ms_to_zero(self) -> None:
        @external_step(saga="s", id="i")
        class ES4:
            pass

        assert ES4.__pyfly_external_step__["backoff_ms"] == 0

    def test_defaults_timeout_ms_to_zero(self) -> None:
        @external_step(saga="s", id="i")
        class ES5:
            pass

        assert ES5.__pyfly_external_step__["timeout_ms"] == 0

    def test_defaults_jitter_to_false(self) -> None:
        @external_step(saga="s", id="i")
        class ES6:
            pass

        assert ES6.__pyfly_external_step__["jitter"] is False

    def test_defaults_jitter_factor_to_zero(self) -> None:
        @external_step(saga="s", id="i")
        class ES7:
            pass

        assert ES7.__pyfly_external_step__["jitter_factor"] == 0.0

    def test_defaults_cpu_bound_to_false(self) -> None:
        @external_step(saga="s", id="i")
        class ES8:
            pass

        assert ES8.__pyfly_external_step__["cpu_bound"] is False

    def test_defaults_compensation_retry_to_none(self) -> None:
        @external_step(saga="s", id="i")
        class ES9:
            pass

        assert ES9.__pyfly_external_step__["compensation_retry"] is None

    def test_defaults_compensation_backoff_ms_to_none(self) -> None:
        @external_step(saga="s", id="i")
        class ES10:
            pass

        assert ES10.__pyfly_external_step__["compensation_backoff_ms"] is None

    def test_defaults_compensation_timeout_ms_to_none(self) -> None:
        @external_step(saga="s", id="i")
        class ES11:
            pass

        assert ES11.__pyfly_external_step__["compensation_timeout_ms"] is None

    def test_defaults_compensation_critical_to_false(self) -> None:
        @external_step(saga="s", id="i")
        class ES12:
            pass

        assert ES12.__pyfly_external_step__["compensation_critical"] is False

    def test_returns_same_class(self) -> None:
        class OriginalExternal:
            pass

        decorated = external_step(saga="s", id="i")(OriginalExternal)
        assert decorated is OriginalExternal

    def test_stores_all_custom_fields(self) -> None:
        @external_step(
            saga="full-saga",
            id="full-step",
            compensate="undo-full",
            depends_on=["prev"],
            retry=2,
            backoff_ms=200,
            timeout_ms=5000,
            jitter=True,
            jitter_factor=0.1,
            cpu_bound=True,
            compensation_retry=1,
            compensation_backoff_ms=100,
            compensation_timeout_ms=3000,
            compensation_critical=True,
        )
        class FullExternal:
            pass

        meta = FullExternal.__pyfly_external_step__
        assert meta["saga"] == "full-saga"
        assert meta["id"] == "full-step"
        assert meta["compensate"] == "undo-full"
        assert meta["depends_on"] == ["prev"]
        assert meta["retry"] == 2
        assert meta["backoff_ms"] == 200
        assert meta["timeout_ms"] == 5000
        assert meta["jitter"] is True
        assert meta["jitter_factor"] == 0.1
        assert meta["cpu_bound"] is True
        assert meta["compensation_retry"] == 1
        assert meta["compensation_backoff_ms"] == 100
        assert meta["compensation_timeout_ms"] == 3000
        assert meta["compensation_critical"] is True


# ---------------------------------------------------------------------------
# Parameter injection markers
# ---------------------------------------------------------------------------


class TestInputMarker:
    def test_instantiation_with_key(self) -> None:
        marker = Input(key="order_id")
        assert marker.key == "order_id"

    def test_instantiation_without_key_defaults_to_none(self) -> None:
        marker = Input()
        assert marker.key is None

    def test_is_frozen(self) -> None:
        marker = Input(key="x")
        with pytest.raises((AttributeError, TypeError)):
            marker.key = "y"  # type: ignore[misc]


class TestFromStepMarker:
    def test_instantiation(self) -> None:
        marker = FromStep(step_id="reserve-inventory")
        assert marker.step_id == "reserve-inventory"

    def test_is_frozen(self) -> None:
        marker = FromStep(step_id="s")
        with pytest.raises((AttributeError, TypeError)):
            marker.step_id = "t"  # type: ignore[misc]


class TestHeaderMarker:
    def test_instantiation(self) -> None:
        marker = Header(name="X-Correlation-ID")
        assert marker.name == "X-Correlation-ID"

    def test_is_frozen(self) -> None:
        marker = Header(name="h")
        with pytest.raises((AttributeError, TypeError)):
            marker.name = "g"  # type: ignore[misc]


class TestVariableMarker:
    def test_instantiation(self) -> None:
        marker = Variable(name="order_total")
        assert marker.name == "order_total"

    def test_is_frozen(self) -> None:
        marker = Variable(name="v")
        with pytest.raises((AttributeError, TypeError)):
            marker.name = "w"  # type: ignore[misc]


class TestSetVariableMarker:
    def test_instantiation(self) -> None:
        marker = SetVariable(name="charge_result")
        assert marker.name == "charge_result"

    def test_is_frozen(self) -> None:
        marker = SetVariable(name="s")
        with pytest.raises((AttributeError, TypeError)):
            marker.name = "t"  # type: ignore[misc]


class TestFromCompensationResultMarker:
    def test_instantiation(self) -> None:
        marker = FromCompensationResult(step_id="charge")
        assert marker.step_id == "charge"

    def test_is_frozen(self) -> None:
        marker = FromCompensationResult(step_id="c")
        with pytest.raises((AttributeError, TypeError)):
            marker.step_id = "d"  # type: ignore[misc]


class TestSentinels:
    def test_headers_is_not_none(self) -> None:
        assert Headers is not None

    def test_variables_is_not_none(self) -> None:
        assert Variables is not None

    def test_compensation_error_is_not_none(self) -> None:
        assert CompensationError is not None

    def test_headers_variables_compensation_error_are_distinct(self) -> None:
        assert Headers is not Variables
        assert Headers is not CompensationError
        assert Variables is not CompensationError


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def reserve_inventory_meta(fn: object) -> dict:
    """Shortcut to read __pyfly_saga_step__ off a decorated function."""
    return fn.__pyfly_saga_step__  # type: ignore[attr-defined]
