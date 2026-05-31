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

from pyfly.cache.ports.outbound import CacheAdapter
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
from pyfly.cqrs.event.publisher import (
    CommandEventPublisher,
    EdaCommandEventPublisher,
    NoOpEventPublisher,
)
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.validation.processor import AutoValidationProcessor
from pyfly.eda.ports.outbound import EventPublisher

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
    * :class:`CommandEventPublisher` (``EdaCommandEventPublisher`` when an EDA
      :class:`~pyfly.eda.ports.outbound.EventPublisher` bean is present, else
      :class:`NoOpEventPublisher`)
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
    def command_event_publisher(self, producer: EventPublisher | None = None) -> CommandEventPublisher:
        # Optional injection: the container supplies the EDA ``EventPublisher``
        # bean when the EDA subsystem is active, otherwise ``producer`` stays
        # ``None`` (see ApplicationContext._call_bean_method default handling).
        # When a real producer exists, domain events emitted by command handlers
        # are forwarded to it; otherwise publishing degrades to a silent no-op.
        if producer is not None:
            return EdaCommandEventPublisher(producer)
        return NoOpEventPublisher()

    @bean
    def command_bus(
        self,
        registry: HandlerRegistry,
        validation: CommandValidationService,
        authorization: AuthorizationService,
        metrics: CqrsMetricsService,
        event_publisher: CommandEventPublisher,
    ) -> DefaultCommandBus:
        return DefaultCommandBus(
            registry=registry,
            validation=validation,
            authorization=authorization,
            metrics=metrics,
            event_publisher=event_publisher,
        )

    @bean
    def query_cache_adapter(self, cache: CacheAdapter | None = None) -> QueryCacheAdapter:
        # Inject the pyfly.cache CacheAdapter bean when the cache subsystem is
        # active; otherwise the adapter degrades to a silent no-op. Previously no
        # CacheAdapter was ever passed, so @cacheable queries were never cached.
        return QueryCacheAdapter(cache=cache)

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
