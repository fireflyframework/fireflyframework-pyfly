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
"""CQRS auto-configuration — wires all CQRS beans into the DI container.

Mirrors Java's ``CqrsAutoConfiguration``.
"""

from __future__ import annotations

import logging

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config
from pyfly.cqrs.authorization.service import AuthorizationService
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.metrics import CqrsMetricsService
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.command.validation import CommandValidationService
from pyfly.cqrs.config.properties import CqrsProperties
from pyfly.cqrs.event.publisher import NoOpEventPublisher
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.validation.processor import AutoValidationProcessor

_logger = logging.getLogger(__name__)


@auto_configuration
@conditional_on_property("pyfly.cqrs.enabled", having_value="true")
class CqrsAutoConfiguration:
    """Auto-configures the CQRS subsystem.

    Creates the following beans:

    * :class:`CqrsProperties`
    * :class:`CorrelationContext`
    * :class:`AutoValidationProcessor`
    * :class:`CommandValidationService`
    * :class:`CqrsMetricsService`
    * :class:`AuthorizationService` (conditional on ``authorization.enabled``)
    * :class:`HandlerRegistry`
    * :class:`DefaultCommandBus`
    * :class:`QueryCacheAdapter` (conditional on cache availability)
    * :class:`DefaultQueryBus`
    """

    @bean
    def cqrs_properties(self, config: Config) -> CqrsProperties:
        return config.bind(CqrsProperties)

    @bean
    def correlation_context(self) -> CorrelationContext:
        return CorrelationContext()

    @bean
    def auto_validation_processor(self) -> AutoValidationProcessor:
        return AutoValidationProcessor()

    @bean
    def command_validation_service(self, processor: AutoValidationProcessor) -> CommandValidationService:
        return CommandValidationService(processor)

    @bean
    def cqrs_metrics_service(self) -> CqrsMetricsService:
        # Metrics registry injected via container if available
        return CqrsMetricsService()

    @bean
    def authorization_service(self, props: CqrsProperties) -> AuthorizationService:
        return AuthorizationService(enabled=props.authorization.enabled)

    @bean
    def handler_registry(self) -> HandlerRegistry:
        return HandlerRegistry()

    @bean
    def command_bus(
        self,
        registry: HandlerRegistry,
        validation: CommandValidationService,
        authorization: AuthorizationService,
        metrics: CqrsMetricsService,
    ) -> DefaultCommandBus:
        return DefaultCommandBus(
            registry=registry,
            validation=validation,
            authorization=authorization,
            metrics=metrics,
            event_publisher=NoOpEventPublisher(),
        )

    @bean
    def query_cache_adapter(self) -> QueryCacheAdapter:
        # When pyfly.cache is available, the CacheAdapter will be injected
        return QueryCacheAdapter()

    @bean
    def query_bus(
        self,
        registry: HandlerRegistry,
        validation: CommandValidationService,
        authorization: AuthorizationService,
        metrics: CqrsMetricsService,
        cache: QueryCacheAdapter,
        props: CqrsProperties,
    ) -> DefaultQueryBus:
        return DefaultQueryBus(
            registry=registry,
            validation=validation,
            authorization=authorization,
            metrics=metrics,
            cache_adapter=cache,
            default_cache_ttl=props.query.cache_ttl,
        )
