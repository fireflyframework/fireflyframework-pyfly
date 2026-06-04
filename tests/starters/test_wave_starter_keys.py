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
"""Regression tests for starter property-key fixes (audit #2/#3/#4/#6).

Each starter bundle must set the property KEY that the corresponding
auto-configuration actually gates on — otherwise the adapters/services never
activate. These tests prove the keys line up with the real conditions (all
optional deps are installed in the test env, so the @conditional_on_class half
also passes).
"""

from __future__ import annotations

from pyfly.actuator.auto_configuration import ActuatorAutoConfiguration
from pyfly.container.container import Container
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.core.application import PyFlyApplication, pyfly_application
from pyfly.core.config import Config
from pyfly.data.document.auto_configuration import DocumentAutoConfiguration
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.security.auto_configuration import JwtAutoConfiguration, PasswordEncoderAutoConfiguration
from pyfly.starters import (
    APPLICATION_STACK_PROPERTIES,
    CORE_STACK_PROPERTIES,
    DATA_STACK_PROPERTIES,
    enable_application_stack,
    enable_core_stack,
    enable_data_stack,
)


def _app(decorator) -> PyFlyApplication:
    @decorator
    @pyfly_application(name="t", scan_packages=[])
    class App: ...

    return PyFlyApplication(App)


def _activates(config: Config, auto_config_cls: type) -> bool:
    """True when the auto-config's pass-1 (on_class + on_property) conditions pass."""
    return ConditionEvaluator(config, Container()).should_include(auto_config_cls)


# ---------------------------------------------------------------------------
# #2 — data starter activates the relational + document adapters
# ---------------------------------------------------------------------------


class TestDataStackKeys:
    def test_uses_pyfly_data_keys(self):
        assert DATA_STACK_PROPERTIES["pyfly.data.relational.enabled"] == "true"
        assert DATA_STACK_PROPERTIES["pyfly.data.document.enabled"] == "true"
        # Old dead keys are gone.
        assert "pyfly.relational.enabled" not in DATA_STACK_PROPERTIES
        assert "pyfly.document.enabled" not in DATA_STACK_PROPERTIES

    def test_relational_and_document_auto_configs_activate(self):
        app = _app(enable_data_stack)
        assert _activates(app.config, RelationalAutoConfiguration)
        assert _activates(app.config, DocumentAutoConfiguration)


# ---------------------------------------------------------------------------
# #3 — core starter actually builds an EventPublisher
# ---------------------------------------------------------------------------


class TestEdaProviderKey:
    def test_core_sets_provider_not_enabled(self):
        assert CORE_STACK_PROPERTIES["pyfly.eda.provider"] == "auto"
        assert "pyfly.eda.enabled" not in CORE_STACK_PROPERTIES

    def test_eda_auto_config_activates_and_builds_bus(self):
        app = _app(enable_core_stack)
        assert _activates(app.config, EdaAutoConfiguration)
        # provider=auto resolves to whichever broker is installed (kafka/redis/pg)
        # or the in-memory bus — the point of #3 is that an EventPublisher is built
        # at all (the old pyfly.eda.enabled key built nothing).
        publisher = EdaAutoConfiguration().event_publisher(app.config)
        assert isinstance(publisher, EventPublisher)


# ---------------------------------------------------------------------------
# #4 — application starter enables the security services
# ---------------------------------------------------------------------------


class TestSecurityKey:
    def test_application_sets_security_enabled(self):
        assert APPLICATION_STACK_PROPERTIES["pyfly.security.enabled"] == "true"
        for dead in ("pyfly.security-jwt.enabled", "pyfly.security-password.enabled", "pyfly.session-filter.enabled"):
            assert dead not in APPLICATION_STACK_PROPERTIES
        # Sessions stay gated on the correct key.
        assert APPLICATION_STACK_PROPERTIES["pyfly.session.enabled"] == "true"

    def test_jwt_and_password_auto_configs_activate(self):
        app = _app(enable_application_stack)
        assert _activates(app.config, JwtAutoConfiguration)
        assert _activates(app.config, PasswordEncoderAutoConfiguration)


# ---------------------------------------------------------------------------
# #6 — starters enable the actuator
# ---------------------------------------------------------------------------


class TestActuatorKey:
    def test_core_sets_web_actuator_enabled(self):
        assert CORE_STACK_PROPERTIES["pyfly.web.actuator.enabled"] == "true"
        assert "pyfly.actuator.enabled" not in CORE_STACK_PROPERTIES
        assert "pyfly.actuator.metrics.enabled" not in CORE_STACK_PROPERTIES

    def test_actuator_auto_config_activates(self):
        app = _app(enable_core_stack)
        assert _activates(app.config, ActuatorAutoConfiguration)
