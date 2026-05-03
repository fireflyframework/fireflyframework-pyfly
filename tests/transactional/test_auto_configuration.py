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
"""Tests for the transactional engine auto-configuration and decorators."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# enable_transactional_engine decorator
# ---------------------------------------------------------------------------


class TestEnableTransactionalEngine:
    """The @enable_transactional_engine decorator sets a marker attribute."""

    def test_sets_marker_attribute(self) -> None:
        from pyfly.transactional.decorators import enable_transactional_engine

        @enable_transactional_engine
        class MyApp:
            pass

        assert getattr(MyApp, "__pyfly_enable_transactional_engine__", False) is True

    def test_returns_same_class(self) -> None:
        from pyfly.transactional.decorators import enable_transactional_engine

        class MyApp:
            pass

        result = enable_transactional_engine(MyApp)
        assert result is MyApp

    def test_class_without_decorator_has_no_marker(self) -> None:
        class PlainApp:
            pass

        assert not hasattr(PlainApp, "__pyfly_enable_transactional_engine__")


# ---------------------------------------------------------------------------
# TransactionalEngineAutoConfiguration structure
# ---------------------------------------------------------------------------


class TestTransactionalEngineAutoConfiguration:
    """Auto-configuration class has the expected structure and metadata."""

    def test_class_exists(self) -> None:
        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        assert TransactionalEngineAutoConfiguration is not None

    def test_has_auto_configuration_marker(self) -> None:
        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        assert (
            getattr(
                TransactionalEngineAutoConfiguration,
                "__pyfly_auto_configuration__",
                False,
            )
            is True
        )

    def test_has_injectable_marker(self) -> None:
        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        assert (
            getattr(
                TransactionalEngineAutoConfiguration,
                "__pyfly_injectable__",
                False,
            )
            is True
        )

    def test_has_configuration_stereotype(self) -> None:
        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        assert (
            getattr(
                TransactionalEngineAutoConfiguration,
                "__pyfly_stereotype__",
                None,
            )
            == "configuration"
        )

    def test_has_conditional_on_property(self) -> None:
        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        conditions = getattr(TransactionalEngineAutoConfiguration, "__pyfly_conditions__", [])
        property_conditions = [c for c in conditions if c.get("type") == "on_property"]
        assert len(property_conditions) >= 1
        assert any(c["key"] == "pyfly.transactional.enabled" for c in property_conditions)


# ---------------------------------------------------------------------------
# Bean methods
# ---------------------------------------------------------------------------


class TestBeanMethods:
    """Auto-configuration defines @bean methods for all required components."""

    EXPECTED_BEAN_METHODS = [
        "saga_engine_properties",
        "tcc_engine_properties",
        "backpressure_properties",
        "in_memory_persistence_adapter",
        "logger_events_adapter",
        "saga_argument_resolver",
        "saga_step_invoker",
        "saga_compensator",
        "saga_execution_orchestrator",
        "saga_engine",
        "saga_registry",
        "tcc_registry",
        "tcc_engine",
        "saga_recovery_service",
    ]

    def test_bean_methods_exist(self) -> None:
        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        for method_name in self.EXPECTED_BEAN_METHODS:
            assert hasattr(TransactionalEngineAutoConfiguration, method_name), f"Missing bean method: {method_name}"

    def test_bean_methods_are_marked(self) -> None:
        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        for method_name in self.EXPECTED_BEAN_METHODS:
            method = getattr(TransactionalEngineAutoConfiguration, method_name)
            assert getattr(method, "__pyfly_bean__", False) is True, (
                f"Method '{method_name}' is not decorated with @bean"
            )

    def test_bean_methods_have_return_type_annotation(self) -> None:
        import inspect

        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        for method_name in self.EXPECTED_BEAN_METHODS:
            method = getattr(TransactionalEngineAutoConfiguration, method_name)
            sig = inspect.signature(method)
            assert sig.return_annotation is not inspect.Parameter.empty, (
                f"Method '{method_name}' has no return type annotation"
            )


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


class TestBeanReturnTypes:
    """Bean methods return the correct component types.

    Because the auto-configuration module uses ``from __future__ import annotations``,
    return annotations are stored as strings.  We use ``get_type_hints`` to resolve
    them to actual types.
    """

    @staticmethod
    def _return_type(method_name: str) -> type:
        """Resolve the return type of a bean method via ``get_type_hints``."""
        from typing import get_type_hints

        from pyfly.transactional.auto_configuration import (
            TransactionalEngineAutoConfiguration,
        )

        method = getattr(TransactionalEngineAutoConfiguration, method_name)
        hints = get_type_hints(method)
        return hints["return"]

    def test_saga_engine_properties_returns_correct_type(self) -> None:
        from pyfly.transactional.saga.config.properties import SagaEngineProperties

        assert self._return_type("saga_engine_properties") is SagaEngineProperties

    def test_tcc_engine_properties_returns_correct_type(self) -> None:
        from pyfly.transactional.tcc.config.properties import TccEngineProperties

        assert self._return_type("tcc_engine_properties") is TccEngineProperties

    def test_backpressure_properties_returns_correct_type(self) -> None:
        from pyfly.transactional.saga.config.properties import BackpressureProperties

        assert self._return_type("backpressure_properties") is BackpressureProperties

    def test_saga_engine_returns_correct_type(self) -> None:
        from pyfly.transactional.saga.engine.saga_engine import SagaEngine

        assert self._return_type("saga_engine") is SagaEngine

    def test_tcc_engine_returns_correct_type(self) -> None:
        from pyfly.transactional.tcc.engine.tcc_engine import TccEngine

        assert self._return_type("tcc_engine") is TccEngine

    def test_saga_registry_returns_correct_type(self) -> None:
        from pyfly.transactional.saga.registry.saga_registry import SagaRegistry

        assert self._return_type("saga_registry") is SagaRegistry

    def test_tcc_registry_returns_correct_type(self) -> None:
        from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry

        assert self._return_type("tcc_registry") is TccRegistry

    def test_persistence_adapter_returns_correct_type(self) -> None:
        from pyfly.transactional.shared.persistence.memory import (
            InMemoryPersistenceAdapter,
        )

        assert self._return_type("in_memory_persistence_adapter") is InMemoryPersistenceAdapter

    def test_logger_events_adapter_returns_correct_type(self) -> None:
        from pyfly.transactional.shared.observability.events import LoggerEventsAdapter

        assert self._return_type("logger_events_adapter") is LoggerEventsAdapter

    def test_saga_recovery_service_returns_correct_type(self) -> None:
        from pyfly.transactional.saga.persistence.recovery import SagaRecoveryService

        assert self._return_type("saga_recovery_service") is SagaRecoveryService


# ---------------------------------------------------------------------------
# Entry point in pyproject.toml
# ---------------------------------------------------------------------------


class TestEntryPoint:
    """Transactional auto-configuration is registered as a pyfly entry point."""

    def test_entry_point_is_registered(self) -> None:
        """Verify entry point is in pyproject.toml by reading it directly."""
        from pathlib import Path

        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        content = pyproject_path.read_text()
        assert "transactional" in content
        assert "pyfly.transactional.auto_configuration:TransactionalEngineAutoConfiguration" in content
