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
"""PyFly web application factory built on Starlette."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from pyfly.container.ordering import get_order
from pyfly.web.adapters.starlette.controller import ControllerRegistrar
from pyfly.web.adapters.starlette.docs import (
    make_openapi_endpoint,
    make_redoc_endpoint,
    make_swagger_ui_endpoint,
)
from pyfly.web.adapters.starlette.errors import global_exception_handler
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import (
    CorrelationFilter,
    RequestContextFilter,
    RequestLoggingFilter,
    SecurityHeadersFilter,
    TransactionIdFilter,
)
from pyfly.web.openapi import OpenAPIGenerator
from pyfly.web.ports.filter import WebFilter
from pyfly.websocket.adapters.starlette import WebSocketRegistrar

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext
    from pyfly.web.cors import CORSConfig


def create_app(
    title: str = "PyFly",
    version: str = "0.1.0",
    description: str = "",
    debug: bool = False,
    context: ApplicationContext | None = None,
    docs_enabled: bool = True,
    extra_routes: list[Route] | None = None,
    actuator_enabled: bool | None = None,
    cors: CORSConfig | None = None,
    lifespan: object | None = None,
) -> Starlette:
    """Create a Starlette application with PyFly enterprise middleware.

    When ``context`` is provided, auto-discovers all ``@rest_controller`` beans
    and mounts their routes.  Also auto-discovers user ``WebFilter``,
    ``ActuatorEndpoint``, ``@websocket_mapping``, and ``@sse_mapping`` beans.

    Includes:
    - WebFilter chain (transaction ID, request logging, security headers, + user filters)
    - Global exception handler (RFC 7807 style)
    - OpenAPI spec, Swagger UI, and ReDoc (when docs_enabled)
    - Actuator endpoints (when actuator_enabled)
    - CORS support (when cors is provided)
    - WebSocket routes (auto-discovered from @websocket_mapping)
    - SSE routes (auto-discovered from @sse_mapping)
    """
    # Detect the admin dashboard early. Its HTTP trace collector is a WebFilter
    # that must join the chain *here*, while the ASGI middleware is being
    # assembled — which happens before ``ApplicationContext.start()`` runs. The
    # auto-configuration bean for it is therefore not yet instantiated, so we
    # create and own the instance directly and hand the same object to the
    # admin route providers below.
    admin_enabled = False
    if context is not None:
        admin_enabled = str(context.config.get("pyfly.admin.enabled", "false")).lower() in ("true", "1", "yes")

    admin_trace_collector = None
    if admin_enabled:
        from pyfly.admin.middleware.trace_collector import TraceCollectorFilter

        admin_trace_collector = TraceCollectorFilter()

    # HTTP auto-instrumentation (Spring Boot `http.server.requests` parity). Like
    # the admin trace collector, the MetricsFilter is a WebFilter that must join
    # the chain HERE while the ASGI middleware is assembled — which happens before
    # ``ApplicationContext.start()`` instantiates beans. Resolving it as a bean
    # would therefore yield ``None`` and silently leave HTTP metrics uncollected,
    # so we own the instance directly (enabled by default, like Spring Boot).
    metrics_filter_instance: WebFilter | None = None
    metrics_filter_type: type | None = None
    if context is not None:
        metrics_enabled = str(context.config.get("pyfly.observability.metrics.enabled", "true")).lower() in (
            "true",
            "1",
            "yes",
        )
        if metrics_enabled:
            try:
                from pyfly.web.adapters.starlette.filters.metrics_filter import MetricsFilter

                # Histogram buckets for http.server.requests (Spring key:
                # management.metrics.distribution.percentiles-histogram.http.server.requests).
                # The meter-name segment contains dots, so read it from the section.
                hist_section = context.config.get_section("pyfly.management.metrics.distribution.percentiles-histogram")
                histogram = str(
                    hist_section.get(
                        "http.server.requests",
                        context.config.get("pyfly.observability.metrics.histogram.enabled", "false"),
                    )
                ).lower() in ("true", "1", "yes")
                metrics_filter_instance = MetricsFilter(histogram=histogram)
                metrics_filter_type = MetricsFilter

                # Register Micrometer-named process/system meters (process_uptime_seconds,
                # process_cpu_usage, system_cpu_count, ...) into the Prometheus registry.
                from pyfly.observability.process_metrics import register_process_metrics

                register_process_metrics()
            except (ImportError, AssertionError):
                metrics_filter_instance = None  # prometheus_client not installed

    # Resolve actuator state early so the httpexchanges recorder filter (if any)
    # can join the chain alongside the metrics filter and trace collector.
    from pyfly.actuator.wiring import make_http_exchange_filter, resolve_actuator_active

    actuator_active = resolve_actuator_active(context, actuator_enabled)
    http_exchange_recorder, http_exchange_filter = make_http_exchange_filter(context, actuator_active)

    # --- Build the WebFilter chain ---
    # RequestContextFilter runs first (HIGHEST_PRECEDENCE) so REQUEST-scoped beans
    # and @pre_authorize/@post_authorize have a live RequestContext to read.
    filters: list[WebFilter] = [
        RequestContextFilter(),
        CorrelationFilter(),
        TransactionIdFilter(),
        RequestLoggingFilter(),
        SecurityHeadersFilter(),
    ]
    if metrics_filter_instance is not None:
        filters.append(metrics_filter_instance)
    if http_exchange_filter is not None:
        filters.append(http_exchange_filter)
    if admin_trace_collector is not None:
        filters.append(admin_trace_collector)

    # Auto-discover user WebFilter beans from context
    if context is not None:
        for _cls, reg in context.container._registrations.items():
            if (
                reg.instance is not None
                and isinstance(reg.instance, WebFilter)
                and not isinstance(
                    reg.instance,
                    (
                        RequestContextFilter,
                        CorrelationFilter,
                        TransactionIdFilter,
                        RequestLoggingFilter,
                        SecurityHeadersFilter,
                    ),
                )
                # The MetricsFilter is owned directly above; skip the bean copy so
                # the request timer is not wired (and counted) twice.
                and not (metrics_filter_type is not None and isinstance(reg.instance, metrics_filter_type))
            ):
                filters.append(reg.instance)

    # Sort all filters by @order (built-in filters use HIGHEST_PRECEDENCE offsets)
    filters.sort(key=lambda f: get_order(type(f)))

    middleware: list[Middleware] = [
        Middleware(WebFilterChainMiddleware, filters=filters),
    ]

    if cors is not None:
        from starlette.middleware.cors import CORSMiddleware

        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=cors.allowed_origins,
                allow_methods=cors.allowed_methods,
                allow_headers=cors.allowed_headers,
                allow_credentials=cors.allow_credentials,
                expose_headers=cors.exposed_headers,
                max_age=cors.max_age,
            )
        )

    routes: list[Route] = []
    registrar = ControllerRegistrar()

    # Auto-discover controller routes from ApplicationContext
    if context is not None:
        routes.extend(registrar.collect_routes(context))

    # Auto-discover WebSocket routes from ApplicationContext
    if context is not None:
        ws_registrar = WebSocketRegistrar()
        routes.extend(ws_registrar.collect_routes(context))  # type: ignore[arg-type]

    # Auto-discover SSE routes from ApplicationContext
    if context is not None:
        from pyfly.web.sse.adapters.starlette import SSERegistrar

        sse_registrar = SSERegistrar()
        routes.extend(sse_registrar.collect_routes(context))

    # Mount OAuth2 login routes when an OAuth2LoginHandler bean exists
    if context is not None:
        from pyfly.security.oauth2.login import OAuth2LoginHandler

        for _cls, reg in context.container._registrations.items():
            if reg.instance is not None and isinstance(reg.instance, OAuth2LoginHandler):
                routes.extend(reg.instance.routes())
                break

    # Append caller-supplied routes (e.g. test helpers)
    if extra_routes:
        routes.extend(extra_routes)

    # Mount actuator endpoints when active (actuator_active resolved above).
    agg = None
    if actuator_active:
        from pyfly.actuator.health import HealthAggregator, HealthIndicator
        from pyfly.actuator.wiring import build_actuator_routes

        agg = HealthAggregator()

        # Auto-discover HealthIndicator beans from context.
        #
        # NOTE: ``create_app`` is typically called BEFORE the ApplicationContext
        # has started (the startup happens inside the ASGI ``lifespan``
        # function). At this point user / auto-configuration beans have
        # been *registered* but not *instantiated*, so the eager loop only
        # finds indicators that were attached as static singletons.
        #
        # The remaining indicators are picked up by ``_install_indicators``
        # which we attach to the Starlette ``on_startup`` hook below — by
        # the time on_startup fires, the lifespan has already triggered
        # ``PyFlyApplication.startup()`` and every bean has been built.
        def _install_indicators() -> None:
            if context is None:
                return
            seen = set(agg._indicators.keys())  # noqa: SLF001 — intentional, indicator names
            for cls, reg in context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, HealthIndicator):
                    indicator_name = reg.name or cls.__name__
                    if indicator_name in seen:
                        continue
                    agg.add_indicator(indicator_name, reg.instance)
                    seen.add(indicator_name)

        _install_indicators()

        routes.extend(build_actuator_routes(context, agg, http_exchange_recorder))

    # Mount admin dashboard when enabled (admin_enabled computed above)
    if admin_enabled and context is not None:
        from pyfly.admin.adapters.starlette import AdminRouteBuilder
        from pyfly.admin.config import AdminProperties
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

        # Use the trace collector that was created above and wired into the
        # filter chain — it is the live instance recording HTTP traffic.
        trace_collector = admin_trace_collector

        # Find view registry from context
        view_registry = AdminViewRegistry()
        for _cls, reg in context.container._registrations.items():
            if reg.instance is not None and isinstance(reg.instance, AdminViewRegistry):
                view_registry = reg.instance
                view_registry.discover_from_context(context)
                break

        # Reuse health aggregator from actuator, or create one for admin
        health_agg = agg
        if health_agg is None:
            from pyfly.actuator.health import HealthAggregator, HealthIndicator

            health_agg = HealthAggregator()
            for cls, reg in context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, HealthIndicator):
                    indicator_name = reg.name or cls.__name__
                    health_agg.add_indicator(indicator_name, reg.instance)

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
        )
        routes.extend(admin_builder.build_routes())  # type: ignore[arg-type]

    # Collect route metadata (used for OpenAPI and startup logging)
    route_metadata = registrar.collect_route_metadata(context) if context is not None else []

    # Generate OpenAPI spec and doc routes
    if docs_enabled:
        generator = OpenAPIGenerator(title=title, version=version, description=description)
        spec = generator.generate(route_metadata or None)

        routes.extend(
            [
                Route("/openapi.json", make_openapi_endpoint(spec)),
                Route("/docs", make_swagger_ui_endpoint(title)),
                Route("/redoc", make_redoc_endpoint(title)),
            ]
        )

    # Health indicators (and other late beans) are only instantiated by
    # ``ApplicationContext.start()``, which runs inside the provided lifespan —
    # AFTER this factory returns. So wrap the caller's lifespan to rerun the
    # indicator scan immediately after startup. Without this, /actuator/health
    # and the admin health view see an empty indicator set and report UP even
    # when a subsystem is DOWN. (When no lifespan is supplied — e.g. tests pass
    # an already-started context — the eager scan above already caught them.)
    effective_lifespan = lifespan
    if actuator_active and lifespan is not None:
        _user_lifespan = lifespan

        @contextlib.asynccontextmanager
        async def _lifespan_with_indicator_rescan(app_: Starlette) -> AsyncIterator[None]:
            async with _user_lifespan(app_):  # type: ignore[operator]
                _install_indicators()  # beans are now instantiated
                yield

        effective_lifespan = _lifespan_with_indicator_rescan

    app = Starlette(
        debug=debug,
        middleware=middleware,
        routes=routes,
        lifespan=effective_lifespan,  # type: ignore[arg-type]
    )

    # Store metadata for startup logging
    app.state.pyfly_route_metadata = route_metadata
    app.state.pyfly_docs_enabled = docs_enabled

    # Also expose the rescan for callers that manage their own lifespan.
    if actuator_active:
        app.state.pyfly_install_health_indicators = _install_indicators

    # Register global exception handler
    app.add_exception_handler(Exception, global_exception_handler)

    return app
