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
"""Actuator auto-configuration — registry, health, and metrics endpoint beans."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

from pyfly.actuator.endpoints.metrics_endpoint import MetricsEndpoint
from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint
from pyfly.actuator.health import HealthAggregator
from pyfly.actuator.registry import ActuatorRegistry
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.core.config import Config


@auto_configuration
@conditional_on_property("pyfly.web.actuator.enabled", having_value="true")
class ActuatorAutoConfiguration:
    """Auto-configures the actuator registry and health aggregator."""

    @bean
    def actuator_registry(self, config: Config) -> ActuatorRegistry:
        return ActuatorRegistry(config=config)

    @bean
    def health_aggregator(self) -> HealthAggregator:
        return HealthAggregator()


@auto_configuration
@conditional_on_property("pyfly.web.actuator.enabled", having_value="true")
@conditional_on_class("prometheus_client")
class MetricsActuatorAutoConfiguration:
    """Auto-configures Prometheus-backed actuator endpoints."""

    @bean
    def metrics_endpoint(self) -> MetricsEndpoint:
        return MetricsEndpoint()

    @bean
    def prometheus_endpoint(self) -> PrometheusEndpoint:
        return PrometheusEndpoint()
