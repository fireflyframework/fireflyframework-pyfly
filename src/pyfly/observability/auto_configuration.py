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
"""Observability auto-configuration — metrics registry and tracer provider beans."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

try:
    from pyfly.observability.metrics import MetricsRegistry
except ImportError:
    MetricsRegistry = object  # type: ignore[misc,assignment]

try:
    from opentelemetry.sdk.trace import TracerProvider
except ImportError:
    TracerProvider = object  # type: ignore[misc,assignment]

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_class
from pyfly.core.config import Config


@auto_configuration
@conditional_on_class("prometheus_client")
class MetricsAutoConfiguration:
    """Auto-configures a MetricsRegistry bean when prometheus_client is installed."""

    @bean
    def metrics_registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    # NOTE: The HTTP ``MetricsFilter`` is NOT registered as a bean. It must join
    # the WebFilter chain while the ASGI app is being assembled in ``create_app``
    # — which runs before ``ApplicationContext.start()`` instantiates beans — so
    # ``create_app`` owns the instance directly (gated on
    # ``pyfly.observability.metrics.enabled``). A bean here would be built too
    # late to ever reach the chain.


@auto_configuration
@conditional_on_class("opentelemetry")
class TracingAutoConfiguration:
    """Auto-configures an OpenTelemetry TracerProvider when opentelemetry is installed."""

    @bean
    def tracer_provider(self, config: Config) -> TracerProvider:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider as _TracerProvider

        service_name = str(
            config.get(
                "pyfly.observability.tracing.service-name",
                config.get("pyfly.app.name", "pyfly-app"),
            )
        )
        resource = Resource.create({"service.name": service_name})
        provider = _TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)
        return provider
