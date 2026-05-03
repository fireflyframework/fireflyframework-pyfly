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
"""Tests for shared transactional port protocols."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pyfly.transactional.shared.ports.outbound import (
    BackpressureStrategyPort,
    CompensationErrorHandlerPort,
    TransactionalEventsPort,
    TransactionalPersistencePort,
)
from pyfly.transactional.shared.types import BackpressureConfig


class TestImports:
    """All 4 protocols must be importable."""

    def test_transactional_persistence_port_importable(self) -> None:
        assert TransactionalPersistencePort is not None

    def test_transactional_events_port_importable(self) -> None:
        assert TransactionalEventsPort is not None

    def test_backpressure_strategy_port_importable(self) -> None:
        assert BackpressureStrategyPort is not None

    def test_compensation_error_handler_port_importable(self) -> None:
        assert CompensationErrorHandlerPort is not None


class TestRuntimeCheckable:
    """All 4 protocols must be runtime_checkable (isinstance must not raise TypeError)."""

    def test_transactional_persistence_port_is_runtime_checkable(self) -> None:
        class _NotAPort:
            pass

        # isinstance must not raise TypeError — that is the runtime_checkable guarantee
        result = isinstance(_NotAPort(), TransactionalPersistencePort)
        assert isinstance(result, bool)

    def test_transactional_events_port_is_runtime_checkable(self) -> None:
        class _NotAPort:
            pass

        result = isinstance(_NotAPort(), TransactionalEventsPort)
        assert isinstance(result, bool)

    def test_backpressure_strategy_port_is_runtime_checkable(self) -> None:
        class _NotAPort:
            pass

        result = isinstance(_NotAPort(), BackpressureStrategyPort)
        assert isinstance(result, bool)

    def test_compensation_error_handler_port_is_runtime_checkable(self) -> None:
        class _NotAPort:
            pass

        result = isinstance(_NotAPort(), CompensationErrorHandlerPort)
        assert isinstance(result, bool)


class TestTransactionalPersistencePortContract:
    """A mock satisfying TransactionalPersistencePort must pass isinstance."""

    def test_mock_satisfies_isinstance(self) -> None:
        class _MockPersistence:
            async def persist_state(self, state: dict[str, Any]) -> None: ...

            async def get_state(self, correlation_id: str) -> dict[str, Any] | None: ...

            async def update_step_status(self, correlation_id: str, step_id: str, status: str) -> None: ...

            async def mark_completed(self, correlation_id: str, successful: bool) -> None: ...

            async def get_in_flight(self) -> list[dict[str, Any]]: ...

            async def get_stale(self, before: datetime) -> list[dict[str, Any]]: ...

            async def cleanup(self, older_than: timedelta) -> int: ...

            async def is_healthy(self) -> bool: ...

        assert isinstance(_MockPersistence(), TransactionalPersistencePort)

    def test_incomplete_mock_does_not_satisfy_isinstance(self) -> None:
        """A class missing required methods must not satisfy the protocol."""

        class _IncompletePersistence:
            async def persist_state(self, state: dict[str, Any]) -> None: ...

            # missing all other methods

        assert not isinstance(_IncompletePersistence(), TransactionalPersistencePort)

    def test_required_method_names(self) -> None:
        expected = {
            "persist_state",
            "get_state",
            "update_step_status",
            "mark_completed",
            "get_in_flight",
            "get_stale",
            "cleanup",
            "is_healthy",
        }
        attrs = {
            name
            for name in dir(TransactionalPersistencePort)
            if not name.startswith("_") and callable(getattr(TransactionalPersistencePort, name, None))
        }
        assert expected.issubset(attrs), f"Missing methods: {expected - attrs}"


class TestTransactionalEventsPortContract:
    """A mock satisfying TransactionalEventsPort must pass isinstance."""

    def test_mock_satisfies_isinstance(self) -> None:
        class _MockEvents:
            async def on_start(self, name: str, correlation_id: str) -> None: ...

            async def on_step_success(
                self,
                name: str,
                correlation_id: str,
                step_id: str,
                attempts: int,
                latency_ms: float,
            ) -> None: ...

            async def on_step_failed(
                self,
                name: str,
                correlation_id: str,
                step_id: str,
                error: Exception,
                attempts: int,
                latency_ms: float,
            ) -> None: ...

            async def on_compensated(
                self,
                name: str,
                correlation_id: str,
                step_id: str,
                error: Exception | None,
            ) -> None: ...

            async def on_completed(self, name: str, correlation_id: str, success: bool) -> None: ...

        assert isinstance(_MockEvents(), TransactionalEventsPort)

    def test_required_method_names(self) -> None:
        expected = {
            "on_start",
            "on_step_success",
            "on_step_failed",
            "on_compensated",
            "on_completed",
        }
        attrs = {
            name
            for name in dir(TransactionalEventsPort)
            if not name.startswith("_") and callable(getattr(TransactionalEventsPort, name, None))
        }
        assert expected.issubset(attrs), f"Missing methods: {expected - attrs}"


class TestBackpressureStrategyPortContract:
    """A mock satisfying BackpressureStrategyPort must pass isinstance."""

    def test_mock_satisfies_isinstance(self) -> None:
        from collections.abc import Awaitable, Callable

        class _MockBackpressure:
            async def apply(
                self,
                items: list[Any],
                processor: Callable[[Any], Awaitable[Any]],
                config: BackpressureConfig,
            ) -> list[Any]: ...

            @property
            def strategy_name(self) -> str:
                return "mock"

        assert isinstance(_MockBackpressure(), BackpressureStrategyPort)

    def test_required_members(self) -> None:
        assert hasattr(BackpressureStrategyPort, "apply")
        assert hasattr(BackpressureStrategyPort, "strategy_name")


class TestCompensationErrorHandlerPortContract:
    """A mock satisfying CompensationErrorHandlerPort must pass isinstance."""

    def test_mock_satisfies_isinstance(self) -> None:
        class _MockCompensationHandler:
            async def handle(self, saga_name: str, step_id: str, error: Exception, ctx: Any) -> None: ...

        assert isinstance(_MockCompensationHandler(), CompensationErrorHandlerPort)

    def test_required_method_names(self) -> None:
        expected = {"handle"}
        attrs = {
            name
            for name in dir(CompensationErrorHandlerPort)
            if not name.startswith("_") and callable(getattr(CompensationErrorHandlerPort, name, None))
        }
        assert expected.issubset(attrs), f"Missing methods: {expected - attrs}"
