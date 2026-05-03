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
"""Tests for TCC registry — TccDefinition, ParticipantDefinition, TccRegistry."""

from __future__ import annotations

import pytest

from pyfly.transactional.tcc.annotations import (
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.registry.participant_definition import (
    ParticipantDefinition,
)
from pyfly.transactional.tcc.registry.tcc_definition import TccDefinition
from pyfly.transactional.tcc.registry.tcc_registry import (
    TccRegistry,
    TccValidationError,
)

# ---------------------------------------------------------------------------
# Fixtures: decorated TCC beans
# ---------------------------------------------------------------------------


@tcc(name="order-tcc", timeout_ms=5000, retry_enabled=True, max_retries=3, backoff_ms=100)
class OrderTccBean:
    """A TCC bean with two participants."""

    @tcc_participant(id="reserve-inventory", order=1, timeout_ms=2000)
    class ReserveInventory:
        @try_method(timeout_ms=1000)
        def try_reserve(self) -> str:
            return "reserved"

        @confirm_method(timeout_ms=500)
        def confirm_reserve(self) -> None:
            pass

        @cancel_method(timeout_ms=500)
        def cancel_reserve(self) -> None:
            pass

    @tcc_participant(id="charge-payment", order=2, timeout_ms=3000, optional=True)
    class ChargePayment:
        @try_method(timeout_ms=1500)
        def try_charge(self) -> str:
            return "charged"

        @confirm_method()
        def confirm_charge(self) -> None:
            pass

        @cancel_method()
        def cancel_charge(self) -> None:
            pass


@tcc(name="simple-tcc")
class SimpleTccBean:
    """A TCC bean with a single participant that has only a try method."""

    @tcc_participant(id="single-step", order=1)
    class SingleStep:
        @try_method()
        def do_try(self) -> str:
            return "tried"


@tcc(name="duplicate-tcc")
class DuplicateIdTccBean:
    """A TCC bean with two participants sharing the same id."""

    @tcc_participant(id="same-id", order=1)
    class First:
        @try_method()
        def try_first(self) -> str:
            return "first"

    @tcc_participant(id="same-id", order=2)
    class Second:
        @try_method()
        def try_second(self) -> str:
            return "second"


@tcc(name="missing-try-tcc")
class MissingTryTccBean:
    """A TCC bean with a participant that has no try method."""

    @tcc_participant(id="no-try", order=1)
    class NoTry:
        @confirm_method()
        def confirm_only(self) -> None:
            pass


class NotATccBean:
    """A plain class without @tcc."""

    pass


# ---------------------------------------------------------------------------
# ParticipantDefinition tests
# ---------------------------------------------------------------------------


class TestParticipantDefinition:
    """Tests for the ParticipantDefinition frozen dataclass."""

    def test_defaults(self) -> None:
        p = ParticipantDefinition(id="test", order=0)
        assert p.id == "test"
        assert p.order == 0
        assert p.timeout_ms == 0
        assert p.optional is False
        assert p.participant_class is None
        assert p.try_method is None
        assert p.confirm_method is None
        assert p.cancel_method is None

    def test_frozen(self) -> None:
        p = ParticipantDefinition(id="test", order=0)
        with pytest.raises(AttributeError):
            p.id = "changed"  # type: ignore[misc]

    def test_with_all_fields(self) -> None:
        def _try() -> None: ...
        def _confirm() -> None: ...
        def _cancel() -> None: ...

        p = ParticipantDefinition(
            id="full",
            order=5,
            timeout_ms=3000,
            optional=True,
            participant_class=object,
            try_method=_try,
            confirm_method=_confirm,
            cancel_method=_cancel,
        )
        assert p.id == "full"
        assert p.order == 5
        assert p.timeout_ms == 3000
        assert p.optional is True
        assert p.participant_class is object
        assert p.try_method is _try
        assert p.confirm_method is _confirm
        assert p.cancel_method is _cancel


# ---------------------------------------------------------------------------
# TccDefinition tests
# ---------------------------------------------------------------------------


class TestTccDefinition:
    """Tests for the TccDefinition dataclass."""

    def test_defaults(self) -> None:
        d = TccDefinition(name="test-tcc", bean=object())
        assert d.name == "test-tcc"
        assert d.timeout_ms == 0
        assert d.retry_enabled is False
        assert d.max_retries == 0
        assert d.backoff_ms == 0
        assert d.participants == {}

    def test_mutable_participants(self) -> None:
        d = TccDefinition(name="test-tcc", bean=object())
        p = ParticipantDefinition(id="p1", order=1)
        d.participants["p1"] = p
        assert "p1" in d.participants


# ---------------------------------------------------------------------------
# TccRegistry tests
# ---------------------------------------------------------------------------


class TestTccRegistry:
    """Tests for the TccRegistry discovery and indexing."""

    def test_register_bean_with_two_participants(self) -> None:
        registry = TccRegistry()
        bean = OrderTccBean()
        definition = registry.register_from_bean(bean)

        assert definition.name == "order-tcc"
        assert definition.bean is bean
        assert definition.timeout_ms == 5000
        assert definition.retry_enabled is True
        assert definition.max_retries == 3
        assert definition.backoff_ms == 100
        assert len(definition.participants) == 2

    def test_participants_discovered_and_ordered(self) -> None:
        registry = TccRegistry()
        bean = OrderTccBean()
        definition = registry.register_from_bean(bean)

        assert "reserve-inventory" in definition.participants
        assert "charge-payment" in definition.participants

        reserve = definition.participants["reserve-inventory"]
        charge = definition.participants["charge-payment"]

        assert reserve.order == 1
        assert charge.order == 2
        assert reserve.timeout_ms == 2000
        assert charge.timeout_ms == 3000
        assert charge.optional is True
        assert reserve.optional is False

    def test_try_confirm_cancel_methods_resolved(self) -> None:
        registry = TccRegistry()
        bean = OrderTccBean()
        definition = registry.register_from_bean(bean)

        reserve = definition.participants["reserve-inventory"]
        assert reserve.try_method is not None
        assert reserve.confirm_method is not None
        assert reserve.cancel_method is not None

        charge = definition.participants["charge-payment"]
        assert charge.try_method is not None
        assert charge.confirm_method is not None
        assert charge.cancel_method is not None

    def test_participant_class_captured(self) -> None:
        registry = TccRegistry()
        bean = OrderTccBean()
        definition = registry.register_from_bean(bean)

        reserve = definition.participants["reserve-inventory"]
        assert reserve.participant_class is OrderTccBean.ReserveInventory

    def test_get_by_name(self) -> None:
        registry = TccRegistry()
        bean = OrderTccBean()
        registry.register_from_bean(bean)

        result = registry.get("order-tcc")
        assert result is not None
        assert result.name == "order-tcc"

    def test_get_missing_returns_none(self) -> None:
        registry = TccRegistry()
        assert registry.get("nonexistent") is None

    def test_get_all(self) -> None:
        registry = TccRegistry()
        registry.register_from_bean(OrderTccBean())
        registry.register_from_bean(SimpleTccBean())

        all_defs = registry.get_all()
        assert len(all_defs) == 2
        names = {d.name for d in all_defs}
        assert names == {"order-tcc", "simple-tcc"}

    def test_duplicate_participant_id_raises(self) -> None:
        registry = TccRegistry()
        bean = DuplicateIdTccBean()
        with pytest.raises(TccValidationError, match="Duplicate participant id 'same-id'"):
            registry.register_from_bean(bean)

    def test_missing_try_method_raises(self) -> None:
        registry = TccRegistry()
        bean = MissingTryTccBean()
        with pytest.raises(TccValidationError, match="must have a @try_method"):
            registry.register_from_bean(bean)

    def test_not_a_tcc_bean_raises(self) -> None:
        registry = TccRegistry()
        bean = NotATccBean()
        with pytest.raises(TccValidationError, match="not decorated with @tcc"):
            registry.register_from_bean(bean)

    def test_simple_bean_with_try_only(self) -> None:
        registry = TccRegistry()
        bean = SimpleTccBean()
        definition = registry.register_from_bean(bean)

        assert len(definition.participants) == 1
        step = definition.participants["single-step"]
        assert step.try_method is not None
        assert step.confirm_method is None
        assert step.cancel_method is None

    def test_participants_sorted_by_order(self) -> None:
        """Participants dict should be ordered by the 'order' field."""
        registry = TccRegistry()
        bean = OrderTccBean()
        definition = registry.register_from_bean(bean)

        participant_ids = list(definition.participants.keys())
        assert participant_ids == ["reserve-inventory", "charge-payment"]
