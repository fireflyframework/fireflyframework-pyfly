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
"""Feature flags auto-configuration (``pyfly.feature-flags.enabled=true``).

Every name a ``@bean`` hint uses is imported at runtime, not under ``TYPE_CHECKING``: the context reads the hints
with ``typing.get_type_hints``, which looks them up in this module's globals. Each bean another bean of the class
consumes is declared and consumed with the same hint (``X | None`` on both sides when it may be absent), which is how
the context orders the methods (a topological sort, ties in name order): properties, provider, resolver, registry,
filter, facade, binding, client. The registry comes before the facade and the binding, which both take it, and the
two lifecycle beans start in creation order within their phase (``FEATURE_FLAGS_PHASE``: after the datasource,
migrations and schema, before the application's lifecycle beans), so the registry has loaded every source before the
binding installs the provider.

The registry holds its ``FeatureFlagsChanged`` events (the boot composition's included) until
``ContextRefreshedEvent``: the context wires the ``@app_event_listener`` methods only after it started the lifecycle
beans, so an event published at start would reach no listener.

Nothing here catches a startup failure: an invalid setting (``ValueError`` naming the key), several OpenFeature
provider beans (:class:`~pyfly.feature_flags.registry.FeatureFlagsError` from the binding's bean method), a source
that must load and cannot (``FlagSourceError``) and a boot composition the provider refuses (``FeatureFlagsError``
from :meth:`FlagRegistry.start <pyfly.feature_flags.registry.FlagRegistry.start>`) each fail the context's start.
"""

from __future__ import annotations

from openfeature.client import OpenFeatureClient
from openfeature.hook import Hook
from openfeature.provider import AbstractProvider

from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.context.environment import Environment
from pyfly.context.events import ApplicationEventPublisher
from pyfly.core.config import Config
from pyfly.feature_flags.client import FeatureFlags, OpenFeatureBinding, client_domain, find_external_provider
from pyfly.feature_flags.context import (
    ApplicationContextContributor,
    EvaluationContextContributor,
    EvaluationContextResolver,
    FeatureFlagsContextFilter,
    SecurityContextContributor,
    TenantContextContributor,
)
from pyfly.feature_flags.hooks import ExposureEventHook, MetricsHook
from pyfly.feature_flags.properties import FeatureFlagsProperties
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.sources import FlagSource
from pyfly.feature_flags.sources.config import ConfigFlagSource
from pyfly.feature_flags.sources.file import FileFlagSource
from pyfly.observability.metrics import MetricsRegistry

__all__ = ["FeatureFlagsAutoConfiguration"]


def _metrics_registry(container: Container) -> MetricsRegistry | None:
    """The ``MetricsRegistry`` bean (only with prometheus_client), looked up at evaluation time: the ``metrics``
    auto-configuration is processed after this one (entry points run in name order)."""
    return container.resolve(MetricsRegistry) if container.contains_type(MetricsRegistry) else None


@auto_configuration
@conditional_on_property("pyfly.feature-flags.enabled", having_value="true")
@conditional_on_class("openfeature")
class FeatureFlagsAutoConfiguration:
    """Wires the flag provider, the registry of sources, the facade and the OpenFeature binding.

    An application bean whose class extends OpenFeature's ``AbstractProvider`` replaces Firefly's provider: the
    registry is then not created, and the binding installs the application's provider instead. Two such beans fail
    startup (which one serves the flags would be ambiguous).
    """

    @bean
    def feature_flags_properties(self, config: Config) -> FeatureFlagsProperties:
        return FeatureFlagsProperties.from_config(config)

    @bean
    @conditional_on_missing_bean(AbstractProvider)
    def firefly_flag_provider(self) -> FireflyFlagProvider | None:
        return FireflyFlagProvider()

    @staticmethod
    def sources(properties: FeatureFlagsProperties, config: Config) -> list[FlagSource]:
        """The enabled sources, lowest precedence first."""
        sources: list[FlagSource] = [ConfigFlagSource.from_config(config)]
        file = properties.sources.file
        if file.enabled:
            interval = properties.seconds(file.refresh_interval, "sources.file.refresh-interval")
            sources.append(FileFlagSource(file.path, refresh_interval=interval))
        return sources

    @bean
    def flag_registry(
        self,
        properties: FeatureFlagsProperties,
        config: Config,
        publisher: ApplicationEventPublisher,
        provider: FireflyFlagProvider | None = None,
    ) -> FlagRegistry | None:
        if provider is None:  # the application declared its own OpenFeature provider
            return None
        return FlagRegistry(self.sources(properties, config), provider, publisher=publisher, hold_events=True)

    @bean
    def evaluation_context_resolver(
        self, properties: FeatureFlagsProperties, config: Config, container: Container
    ) -> EvaluationContextResolver:
        builtins: list[EvaluationContextContributor] = [
            SecurityContextContributor(),
            TenantContextContributor(
                tenant_attribute=properties.context.tenant_attribute,
                trust_tenant_header=properties.context.trust_tenant_header,
            ),
            ApplicationContextContributor(
                application=str(config.get("pyfly.app.name", "pyfly-app")),
                profiles=Environment(config).active_profiles,
            ),
        ]
        return EvaluationContextResolver(builtins, container=container)

    @bean
    def feature_flags_context_filter(self, resolver: EvaluationContextResolver) -> FeatureFlagsContextFilter:
        return FeatureFlagsContextFilter(resolver)

    @staticmethod
    def framework_client(
        properties: FeatureFlagsProperties, publisher: ApplicationEventPublisher, container: Container
    ) -> OpenFeatureClient:
        """The framework's client, with the Firefly hooks. The metrics hook is always attached (it counts nothing
        without a ``MetricsRegistry``); the exposure hook publishes on every evaluation, so it is attached only with
        ``events.evaluations``."""
        hooks: list[Hook] = [MetricsHook(lambda: _metrics_registry(container))]
        if properties.events.evaluations:
            hooks.append(ExposureEventHook(publisher))
        return OpenFeatureClient(domain=client_domain(properties.openfeature.domain), version=None, hooks=hooks)

    @bean
    def feature_flags(
        self,
        properties: FeatureFlagsProperties,
        publisher: ApplicationEventPublisher,
        container: Container,
        resolver: EvaluationContextResolver,
        registry: FlagRegistry | None = None,
    ) -> FeatureFlags:
        """The facade, over the framework's own client (never an application's ``OpenFeatureClient`` bean, which may
        be bound to another domain and carries no Firefly hooks)."""
        return FeatureFlags(self.framework_client(properties, publisher, container), resolver, registry=registry)

    @bean
    @conditional_on_missing_bean(OpenFeatureClient)
    def open_feature_client(self, facade: FeatureFlags) -> OpenFeatureClient:
        """The framework's client as a bean, unless the application declares its own ``OpenFeatureClient`` (which
        then is the one bean of that type, so injecting it is never ambiguous; the facade keeps the framework's)."""
        return facade.client

    @bean
    def open_feature_binding(
        self,
        properties: FeatureFlagsProperties,
        facade: FeatureFlags,
        container: Container,
        provider: FireflyFlagProvider | None = None,
        registry: FlagRegistry | None = None,
    ) -> OpenFeatureBinding:
        """*registry* is unused on purpose: it orders the binding after the registry (creation order is lifecycle
        start order). ``find_external_provider`` raises ``FeatureFlagsError`` for several provider beans."""
        installed = provider if provider is not None else find_external_provider(container)
        return OpenFeatureBinding(
            installed,
            facade,
            domain=properties.openfeature.domain or None,
            disabled_status=properties.web.disabled_status,
        )
