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
"""Shared admin dashboard wiring used by the web adapters and the management app.

Keeps the Starlette/FastAPI adapters (and the separate management app) from
drifting: one place builds the admin routes from the application context. Mirrors
``pyfly.actuator.wiring``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.actuator.health import HealthAggregator
    from pyfly.context.application_context import ApplicationContext


def build_admin_routes(
    context: ApplicationContext,
    *,
    admin_trace_collector: Any | None,
    base_health_agg: HealthAggregator | None,
    extra_post_start: list[Callable[[], None]],
) -> list[Any]:
    """Build the admin dashboard routes for a (started) application context.

    *admin_trace_collector* is the live ``TraceCollectorFilter`` owned by the web
    adapter's filter chain. *base_health_agg* is the actuator's health aggregator
    to reuse (or ``None`` to create a dedicated one whose indicator scan is
    appended to *extra_post_start*). Returns Starlette ``Route`` objects (typed
    ``Any`` to keep Starlette imports inside the adapters).
    """
    from pyfly.admin.adapters.starlette import AdminRouteBuilder
    from pyfly.admin.config import AdminProperties, AdminServerProperties
    from pyfly.admin.providers.beans_provider import BeansProvider
    from pyfly.admin.providers.cache_provider import CacheProvider
    from pyfly.admin.providers.config_provider import ConfigProvider
    from pyfly.admin.providers.cqrs_provider import CqrsProvider
    from pyfly.admin.providers.env_provider import EnvProvider
    from pyfly.admin.providers.health_provider import HealthProvider
    from pyfly.admin.providers.logfile_provider import LogfileProvider
    from pyfly.admin.providers.loggers_provider import LoggersProvider
    from pyfly.admin.providers.mappings_provider import MappingsProvider
    from pyfly.admin.providers.metrics_provider import MetricsProvider
    from pyfly.admin.providers.overview_provider import OverviewProvider
    from pyfly.admin.providers.runtime_provider import RuntimeProvider
    from pyfly.admin.providers.scheduled_provider import ScheduledProvider
    from pyfly.admin.providers.server_provider import ServerProvider
    from pyfly.admin.providers.traces_provider import TracesProvider
    from pyfly.admin.providers.transactions_provider import TransactionsProvider
    from pyfly.admin.registry import AdminViewRegistry

    admin_props = AdminProperties()
    with contextlib.suppress(Exception):
        admin_props = context.config.bind(AdminProperties)

    # Server mode: build the instance registry and seed it from statically
    # configured instances so the /api/instances routes are mounted and
    # serverMode reports true (audit #67). Background health pollers are not
    # wired here (deferred).
    admin_instance_registry = None
    server_props = AdminServerProperties()
    with contextlib.suppress(Exception):
        server_props = context.config.bind(AdminServerProperties)
    if server_props.enabled:
        from pyfly.admin.server.discovery import StaticDiscovery
        from pyfly.admin.server.instance_registry import InstanceRegistry

        admin_instance_registry = InstanceRegistry()
        StaticDiscovery(server_props.instances, admin_instance_registry).discover()

    # Use the trace collector that was created by the adapter and wired into the
    # filter chain — it is the live instance recording HTTP traffic.
    trace_collector = admin_trace_collector

    # Find view registry from context.
    view_registry = AdminViewRegistry()
    for _cls, reg in context.container._registrations.items():
        if reg.instance is not None and isinstance(reg.instance, AdminViewRegistry):
            view_registry = reg.instance
            view_registry.discover_from_context(context)
            break

    # Reuse the actuator health aggregator, or create one for admin.
    health_agg = base_health_agg
    if health_agg is None:
        from pyfly.actuator.health import HealthAggregator
        from pyfly.actuator.wiring import install_health_indicators

        health_agg = HealthAggregator()

        def _install_admin_indicators(_agg: HealthAggregator = health_agg) -> None:
            # HealthIndicator beans are only instantiated during start(); rescan
            # post-start so the admin health view isn't a frozen empty pre-startup
            # snapshot when the actuator is disabled (audit #70).
            install_health_indicators(context, _agg)

        _install_admin_indicators()
        extra_post_start.append(_install_admin_indicators)

    admin_builder = AdminRouteBuilder(
        properties=admin_props,
        overview=OverviewProvider(context, health_agg),
        beans=BeansProvider(context),
        health=HealthProvider(health_agg),
        env=EnvProvider(context),
        config=ConfigProvider(context),
        loggers=LoggersProvider(),
        metrics=MetricsProvider(),
        scheduled=ScheduledProvider(context),
        mappings=MappingsProvider(context),
        caches=CacheProvider(context),
        cqrs=CqrsProvider(context),
        transactions=TransactionsProvider(context),
        traces=TracesProvider(trace_collector),
        view_registry=view_registry,
        trace_collector=trace_collector,
        logfile=LogfileProvider(context),
        runtime=RuntimeProvider(),
        server=ServerProvider(context=context),
        instance_registry=admin_instance_registry,
    )
    return list(admin_builder.build_routes())
