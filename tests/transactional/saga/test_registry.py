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
"""Tests for saga registry — metadata discovery and DAG validation."""

from __future__ import annotations

import pytest

from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
from pyfly.transactional.saga.registry.saga_registry import (
    SagaRegistry,
    SagaValidationError,
)
from pyfly.transactional.saga.registry.step_definition import StepDefinition

# -- Test fixtures -----------------------------------------------------------


@saga(name="order-saga")
class OrderSaga:
    @saga_step(id="validate", compensate=None)
    async def validate_order(self) -> str:
        return "valid"

    @saga_step(id="reserve", compensate="release", depends_on=["validate"])
    async def reserve_inventory(self) -> str:
        return "reserved"

    async def release(self) -> None:
        pass

    @saga_step(id="charge", compensate="refund", depends_on=["reserve"])
    async def charge_payment(self) -> str:
        return "charged"

    async def refund(self) -> None:
        pass


@saga(name="cyclic-saga")
class CyclicSaga:
    @saga_step(id="a", depends_on=["b"])
    async def step_a(self) -> None:
        pass

    @saga_step(id="b", depends_on=["a"])
    async def step_b(self) -> None:
        pass


@saga(name="missing-dep-saga")
class MissingDepSaga:
    @saga_step(id="x", depends_on=["nonexistent"])
    async def step_x(self) -> None:
        pass


# -- Tests -------------------------------------------------------------------


class TestStepDefinition:
    def test_create_from_metadata(self) -> None:
        defn = StepDefinition(
            id="step-1",
            compensate_name="undo",
            depends_on=["step-0"],
            retry=3,
            backoff_ms=100,
            timeout_ms=5000,
        )
        assert defn.id == "step-1"
        assert defn.compensate_name == "undo"
        assert defn.depends_on == ["step-0"]


class TestSagaDefinition:
    def test_create(self) -> None:
        defn = SagaDefinition(name="test", bean=object(), layer_concurrency=5)
        assert defn.name == "test"
        assert defn.layer_concurrency == 5
        assert defn.steps == {}


class TestSagaRegistry:
    def test_register_from_beans(self) -> None:
        registry = SagaRegistry()
        bean = OrderSaga()
        registry.register_from_bean(bean)
        defn = registry.get("order-saga")
        assert defn is not None
        assert defn.name == "order-saga"
        assert "validate" in defn.steps
        assert "reserve" in defn.steps
        assert "charge" in defn.steps

    def test_step_dependencies_extracted(self) -> None:
        registry = SagaRegistry()
        registry.register_from_bean(OrderSaga())
        defn = registry.get("order-saga")
        assert defn is not None
        assert defn.steps["reserve"].depends_on == ["validate"]
        assert defn.steps["charge"].depends_on == ["reserve"]

    def test_compensation_method_resolved(self) -> None:
        registry = SagaRegistry()
        registry.register_from_bean(OrderSaga())
        defn = registry.get("order-saga")
        assert defn is not None
        assert defn.steps["reserve"].compensate_name == "release"
        assert defn.steps["reserve"].compensate_method is not None

    def test_cyclic_dependency_raises(self) -> None:
        registry = SagaRegistry()
        with pytest.raises(SagaValidationError, match="cycle"):
            registry.register_from_bean(CyclicSaga())

    def test_missing_dependency_raises(self) -> None:
        registry = SagaRegistry()
        with pytest.raises(SagaValidationError, match="nonexistent"):
            registry.register_from_bean(MissingDepSaga())

    def test_get_all(self) -> None:
        registry = SagaRegistry()
        registry.register_from_bean(OrderSaga())
        all_sagas = registry.get_all()
        assert len(all_sagas) == 1
        assert all_sagas[0].name == "order-saga"

    def test_get_missing_returns_none(self) -> None:
        registry = SagaRegistry()
        assert registry.get("nonexistent") is None
