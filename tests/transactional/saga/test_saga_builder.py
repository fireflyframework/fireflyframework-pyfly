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
"""Tests for SagaBuilder — fluent DSL for programmatic saga creation."""

from __future__ import annotations

import pytest

from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.registry.saga_builder import SagaBuilder
from pyfly.transactional.saga.registry.saga_registry import SagaValidationError

# ── Helper functions ──────────────────────────────────────────────────


async def validate_fn(ctx: SagaContext) -> str:
    return "valid"


async def reserve_fn(ctx: SagaContext) -> str:
    return "reserved"


async def release_fn(ctx: SagaContext) -> None:
    pass


async def charge_fn(ctx: SagaContext) -> str:
    return "charged"


async def refund_fn(ctx: SagaContext) -> None:
    pass


async def notify_fn(ctx: SagaContext) -> str:
    return "notified"


# ── Tests ─────────────────────────────────────────────────────────────


class TestSagaBuilderSingleStep:
    def test_basic_builder_with_single_step(self) -> None:
        saga_def = SagaBuilder("simple-saga").step("validate").handler(validate_fn).add().build()

        assert saga_def.name == "simple-saga"
        assert len(saga_def.steps) == 1
        assert "validate" in saga_def.steps
        assert saga_def.steps["validate"].step_method is validate_fn
        assert saga_def.steps["validate"].depends_on == []

    def test_single_step_defaults(self) -> None:
        saga_def = SagaBuilder("defaults-saga").step("s1").handler(validate_fn).add().build()

        step = saga_def.steps["s1"]
        assert step.retry == 0
        assert step.backoff_ms == 0
        assert step.timeout_ms == 0
        assert step.jitter is False
        assert step.jitter_factor == 0.0
        assert step.cpu_bound is False
        assert step.compensate_method is None

    def test_bean_is_none_for_builder_sagas(self) -> None:
        saga_def = SagaBuilder("no-bean").step("s1").handler(validate_fn).add().build()
        assert saga_def.bean is None


class TestSagaBuilderMultipleSteps:
    def test_multiple_steps_with_dependencies(self) -> None:
        saga_def = (
            SagaBuilder("multi-saga")
            .step("validate")
            .handler(validate_fn)
            .add()
            .step("reserve")
            .handler(reserve_fn)
            .depends_on("validate")
            .add()
            .step("charge")
            .handler(charge_fn)
            .depends_on("reserve")
            .add()
            .build()
        )

        assert len(saga_def.steps) == 3
        assert saga_def.steps["validate"].depends_on == []
        assert saga_def.steps["reserve"].depends_on == ["validate"]
        assert saga_def.steps["charge"].depends_on == ["reserve"]

    def test_multiple_dependencies(self) -> None:
        saga_def = (
            SagaBuilder("fan-in")
            .step("a")
            .handler(validate_fn)
            .add()
            .step("b")
            .handler(reserve_fn)
            .add()
            .step("c")
            .handler(charge_fn)
            .depends_on("a", "b")
            .add()
            .build()
        )

        assert set(saga_def.steps["c"].depends_on) == {"a", "b"}

    def test_diamond_dependency_graph(self) -> None:
        saga_def = (
            SagaBuilder("diamond")
            .step("root")
            .handler(validate_fn)
            .add()
            .step("left")
            .handler(reserve_fn)
            .depends_on("root")
            .add()
            .step("right")
            .handler(charge_fn)
            .depends_on("root")
            .add()
            .step("join")
            .handler(notify_fn)
            .depends_on("left", "right")
            .add()
            .build()
        )

        assert len(saga_def.steps) == 4
        assert saga_def.steps["root"].depends_on == []
        assert saga_def.steps["left"].depends_on == ["root"]
        assert saga_def.steps["right"].depends_on == ["root"]
        assert set(saga_def.steps["join"].depends_on) == {"left", "right"}


class TestSagaBuilderStepConfiguration:
    def test_all_step_configuration_options(self) -> None:
        saga_def = (
            SagaBuilder("configured-saga")
            .step("reserve")
            .handler(reserve_fn)
            .compensate(release_fn)
            .depends_on("validate")
            .retry(3)
            .backoff_ms(100)
            .timeout_ms(5000)
            .jitter(True, 0.3)
            .cpu_bound(True)
            .add()
            .step("validate")
            .handler(validate_fn)
            .add()
            .build()
        )

        step = saga_def.steps["reserve"]
        assert step.step_method is reserve_fn
        assert step.compensate_method is release_fn
        assert step.depends_on == ["validate"]
        assert step.retry == 3
        assert step.backoff_ms == 100
        assert step.timeout_ms == 5000
        assert step.jitter is True
        assert step.jitter_factor == 0.3
        assert step.cpu_bound is True

    def test_compensation_methods(self) -> None:
        saga_def = (
            SagaBuilder("comp-saga")
            .step("reserve")
            .handler(reserve_fn)
            .compensate(release_fn)
            .add()
            .step("charge")
            .handler(charge_fn)
            .compensate(refund_fn)
            .add()
            .build()
        )

        assert saga_def.steps["reserve"].compensate_method is release_fn
        assert saga_def.steps["charge"].compensate_method is refund_fn

    def test_retry_only(self) -> None:
        saga_def = SagaBuilder("retry-saga").step("s1").handler(validate_fn).retry(5).add().build()

        assert saga_def.steps["s1"].retry == 5
        assert saga_def.steps["s1"].backoff_ms == 0

    def test_jitter_defaults(self) -> None:
        saga_def = SagaBuilder("jitter-saga").step("s1").handler(validate_fn).jitter().add().build()

        step = saga_def.steps["s1"]
        assert step.jitter is True
        assert step.jitter_factor == 0.5

    def test_cpu_bound_default(self) -> None:
        saga_def = SagaBuilder("cpu-saga").step("s1").handler(validate_fn).cpu_bound().add().build()

        assert saga_def.steps["s1"].cpu_bound is True


class TestSagaBuilderLayerConcurrency:
    def test_layer_concurrency(self) -> None:
        saga_def = (
            SagaBuilder("concurrent-saga").step("validate").handler(validate_fn).add().layer_concurrency(5).build()
        )

        assert saga_def.layer_concurrency == 5

    def test_default_layer_concurrency(self) -> None:
        saga_def = SagaBuilder("default-conc").step("s1").handler(validate_fn).add().build()

        assert saga_def.layer_concurrency == 0


class TestSagaBuilderValidation:
    def test_build_raises_on_empty_saga(self) -> None:
        with pytest.raises(SagaValidationError, match="at least one step"):
            SagaBuilder("empty").build()

    def test_build_raises_on_missing_dependency(self) -> None:
        with pytest.raises(SagaValidationError, match="nonexistent"):
            (SagaBuilder("bad-dep").step("s1").handler(validate_fn).depends_on("ghost").add().build())

    def test_build_raises_on_cycle(self) -> None:
        with pytest.raises(SagaValidationError, match="cycle"):
            (
                SagaBuilder("cyclic")
                .step("a")
                .handler(validate_fn)
                .depends_on("b")
                .add()
                .step("b")
                .handler(reserve_fn)
                .depends_on("a")
                .add()
                .build()
            )

    def test_build_raises_on_self_dependency(self) -> None:
        with pytest.raises(SagaValidationError, match="cycle"):
            (SagaBuilder("self-dep").step("a").handler(validate_fn).depends_on("a").add().build())

    def test_duplicate_step_id_raises(self) -> None:
        with pytest.raises(SagaValidationError, match="duplicate|already exists"):
            (SagaBuilder("dup").step("s1").handler(validate_fn).add().step("s1").handler(reserve_fn).add().build())

    def test_step_without_handler_raises(self) -> None:
        with pytest.raises(SagaValidationError, match="handler"):
            (SagaBuilder("no-handler").step("s1").add().build())


class TestSagaBuilderEndToEnd:
    def test_full_example_from_task_description(self) -> None:
        saga_def = (
            SagaBuilder("my-saga")
            .step("validate")
            .handler(validate_fn)
            .add()
            .step("reserve")
            .handler(reserve_fn)
            .compensate(release_fn)
            .depends_on("validate")
            .retry(3)
            .backoff_ms(100)
            .timeout_ms(5000)
            .add()
            .layer_concurrency(5)
            .build()
        )

        assert saga_def.name == "my-saga"
        assert saga_def.layer_concurrency == 5
        assert "validate" in saga_def.steps
        assert "reserve" in saga_def.steps
        assert saga_def.steps["reserve"].depends_on == ["validate"]
        assert saga_def.steps["reserve"].retry == 3
        assert saga_def.steps["reserve"].compensate_method is not None

    def test_chaining_returns_correct_types(self) -> None:
        builder = SagaBuilder("chain-test")
        step_builder = builder.step("s1")
        # StepBuilder methods return StepBuilder for chaining
        assert step_builder.handler(validate_fn) is step_builder
        assert step_builder.retry(1) is step_builder
        assert step_builder.backoff_ms(50) is step_builder
        assert step_builder.timeout_ms(1000) is step_builder
        assert step_builder.jitter() is step_builder
        assert step_builder.cpu_bound() is step_builder
        assert step_builder.compensate(release_fn) is step_builder
        assert step_builder.depends_on("other") is step_builder
        # add() returns the parent SagaBuilder
        returned = step_builder.add()
        assert returned is builder
