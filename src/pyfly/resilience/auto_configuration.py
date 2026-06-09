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
"""Resilience subsystem auto-configuration.

Registers a :class:`~pyfly.resilience.registry.ResilienceRegistry` bean that
holds all named resilience instances declared in ``pyfly.resilience.*``
configuration keys (circuit-breakers, rate-limiters, bulkheads, time-limiters).

The registry is always-on (no ``@conditional_on_property`` gate) because it is
cheap and returns an empty registry when no resilience keys are configured.
"""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration
from pyfly.core.config import Config
from pyfly.resilience.registry import ResilienceRegistry


@auto_configuration
class ResilienceAutoConfiguration:
    """Auto-configures the :class:`~pyfly.resilience.registry.ResilienceRegistry`
    from ``pyfly.resilience.*`` properties.

    Inject the registry wherever named resilience instances are needed::

        @service
        class PaymentService:
            def __init__(self, registry: ResilienceRegistry) -> None:
                self._cb = registry.circuit_breaker("payment-api")
                self._rl = registry.rate_limiter("payment-api")
    """

    @bean
    def resilience_registry(self, config: Config) -> ResilienceRegistry:
        """Build the :class:`~pyfly.resilience.registry.ResilienceRegistry`
        from the application configuration."""
        return ResilienceRegistry.from_config(config)
