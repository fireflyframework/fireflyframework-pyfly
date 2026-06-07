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
"""PyFly web application factory built on FastAPI."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.routing import Route

from pyfly.container.ordering import get_order
from pyfly.web.adapters.fastapi.controller import FastAPIControllerRegistrar
from pyfly.web.adapters.fastapi.errors import register_exception_handlers
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import (
    CorrelationFilter,
    RequestContextFilter,
    RequestLoggingFilter,
    SecurityHeadersFilter,
    TracingFilter,
    TransactionIdFilter,
)
from pyfly.web.openapi import OpenAPIGenerator
from pyfly.web.ports.filter import WebFilter

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
) -> FastAPI:
    """Create a FastAPI application with PyFly enterprise middleware.

    When ``context`` is provided, auto-discovers all ``@rest_controller`` beans
    and mounts their routes.  Also auto-discovers user ``WebFilter``,
    ``ActuatorEndpoint``, ``@websocket_mapping``, and ``@sse_mapping`` beans.

    FastAPI's own introspection cannot read the PyFly parameter-binding metadata
    on controller handlers, so controller routes are registered with
    ``include_in_schema=False`` and the OpenAPI document is generated with the
    same :class:`pyfly.web.openapi.OpenAPIGenerator` machinery used by the
    Starlette adapter. FastAPI's built-in ``/docs`` and ``/redoc`` pages still
    render, but read from this generated spec.

    Includes:
    - WebFilter chain (transaction ID, request logging, security headers, + user filters)
    - Global exception handler (RFC 7807 style)
    - Built-in Swagger UI and ReDoc (when docs_enabled)
    - Actuator endpoints (when actuator_enabled)
    - Admin dashboard (when pyfly.admin.enabled config is set)
    - CORS support (when cors is provided)
    - WebSocket routes (auto-discovered from @websocket_mapping)
    - SSE routes (auto-discovered from @sse_mapping)
    """
    # Detect the admin dashboard early so its HTTP trace collector can join the
    # filter chain here (before ApplicationContext.start() instantiates beans).
    # See the Starlette adapter for the full rationale.
    admin_enabled = False
    if context is not None:
        admin_enabled = str(context.config.get("pyfly.admin.enabled", "false")).lower() in ("true", "1", "yes")

    admin_trace_collector = None
    if admin_enabled:
        from pyfly.admin.middleware.trace_collector import TraceCollectorFilter

        admin_trace_collector = TraceCollectorFilter()

    # HTTP auto-instrumentation (Spring `http.server.requests` parity). Owned
    # directly here because the bean is built too late to reach the chain.
    metrics_filter_instance: WebFilter | None = None
    metrics_filter_type: type | None = None
    if context is not None and str(context.config.get("pyfly.observability.metrics.enabled", "true")).lower() in (
        "true",
        "1",
        "yes",
    ):
        try:
            from pyfly.web.adapters.starlette.filters.metrics_filter import MetricsFilter

            hist_section = context.config.get_section("pyfly.management.metrics.distribution.percentiles-histogram")
            histogram = str(
                hist_section.get(
                    "http.server.requests",
                    context.config.get("pyfly.observability.metrics.histogram.enabled", "false"),
                )
            ).lower() in ("true", "1", "yes")
            metrics_filter_instance = MetricsFilter(histogram=histogram)
            metrics_filter_type = MetricsFilter

            from pyfly.observability.process_metrics import register_process_metrics

            register_process_metrics()
        except (ImportError, AssertionError):
            metrics_filter_instance = None

    # Resolve actuator state early so the httpexchanges recorder filter can join.
    from pyfly.actuator.wiring import make_http_exchange_filter, resolve_actuator_active

    actuator_active = resolve_actuator_active(context, actuator_enabled)
    http_exchange_recorder, http_exchange_filter = make_http_exchange_filter(context, actuator_active)

    # --- Build the WebFilter chain ---
    # RequestContextFilter runs first (HIGHEST_PRECEDENCE) so REQUEST-scoped beans
    # and @pre_authorize/@post_authorize have a live RequestContext to read.
    filters: list[WebFilter] = [
        RequestContextFilter(),
        CorrelationFilter(),
        TracingFilter(),
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

    builtin_filter_types = (
        RequestContextFilter,
        CorrelationFilter,
        TracingFilter,
        TransactionIdFilter,
        RequestLoggingFilter,
        SecurityHeadersFilter,
    )

    def _install_user_filters() -> None:
        """Append user ``WebFilter`` beans not already in the live chain.

        Re-runnable after ApplicationContext.start() so security/session/CSRF
        filter beans actually join the chain (audit #40).
        """
        if context is None:
            return
        present = {id(f) for f in filters}
        added = False
        for _cls, reg in context.container._registrations.items():
            inst = reg.instance
            if (
                inst is not None
                and id(inst) not in present
                and isinstance(inst, WebFilter)
                and not isinstance(inst, builtin_filter_types)
                and not (metrics_filter_type is not None and isinstance(inst, metrics_filter_type))
            ):
                filters.append(inst)
                present.add(id(inst))
                added = True
        if added:
            filters.sort(key=lambda f: get_order(type(f)))

    _install_user_filters()

    middleware: list[Middleware] = [
        Middleware(WebFilterChainMiddleware, filters=filters),
    ]

    # CORS auto-configuration (audit #204): build a CORSConfig from
    # ``pyfly.web.cors.*`` when none is passed, matching Spring's
    # CorsAutoConfiguration. Secure-by-default: disabled unless opted in.
    if cors is None and context is not None:
        from pyfly.web.cors import CORSConfig as _CORSConfig

        cors = _CORSConfig.from_config(context.config)

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

    # Configure OpenAPI docs URLs (None disables them)
    docs_url = "/docs" if docs_enabled else None
    redoc_url = "/redoc" if docs_enabled else None
    openapi_url = "/openapi.json" if docs_enabled else None

    app = FastAPI(
        title=title,
        version=version,
        description=description,
        debug=debug,
        middleware=middleware,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        lifespan=lifespan,  # type: ignore[arg-type]
    )

    # Auto-discover and register controller routes from ApplicationContext.
    # Routes are registered with include_in_schema=False; the OpenAPI document
    # is built below from collected route metadata instead.
    registrar = FastAPIControllerRegistrar()

    def _install_context_routes() -> None:
        """Register controller / websocket / SSE / OAuth2-login routes.

        Re-runnable (each step is idempotent / deduplicated) so @bean-produced
        controllers and OAuth2 login handlers registered only during start are
        mounted after the lifespan starts the context (audit #163/#44).
        """
        if context is None:
            return
        registrar.register_controllers(app, context)  # idempotent

        existing = {(getattr(r, "path", ""), frozenset(getattr(r, "methods", None) or ())) for r in app.routes}

        def _add(new_routes: object) -> None:
            for r in new_routes:  # type: ignore[attr-defined]
                key = (getattr(r, "path", ""), frozenset(getattr(r, "methods", None) or ()))
                if key not in existing:
                    app.routes.append(r)
                    existing.add(key)

        from pyfly.web.sse.adapters.starlette import SSERegistrar
        from pyfly.websocket.adapters.starlette import WebSocketRegistrar

        _add(WebSocketRegistrar().collect_routes(context))
        _add(SSERegistrar().collect_routes(context))

        # OAuth2 login routes exist only when the security extra (pyjwt) is
        # installed and an OAuth2LoginHandler bean is registered. Import lazily and
        # tolerate its absence so a web-only install (pyfly[web], no pyfly[security])
        # still boots — otherwise create_app() crashes on `import jwt`.
        oauth2_login_cls: type | None
        try:
            from pyfly.security.oauth2.login import OAuth2LoginHandler

            oauth2_login_cls = OAuth2LoginHandler
        except ImportError:
            oauth2_login_cls = None

        if oauth2_login_cls is not None:
            for _cls, reg in context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, oauth2_login_cls):
                    _add(reg.instance.routes())
                    break

        # Mount config-server routes once its ConfigServer bean exists (#83).
        from pyfly.config_server.wiring import build_config_server_routes

        _add(build_config_server_routes(context))

    _install_context_routes()

    # Append caller-supplied routes (e.g. test helpers)
    if extra_routes:
        app.routes.extend(extra_routes)

    # Post-start hooks run once after ApplicationContext.start() (inside the
    # lifespan), when every bean is finally instantiated.
    _extra_post_start: list[Callable[[], None]] = []

    # Mount actuator endpoints when active (actuator_active resolved above).
    agg = None
    if actuator_active:
        from pyfly.actuator.health import HealthAggregator, HealthIndicator
        from pyfly.actuator.wiring import build_actuator_routes

        agg = HealthAggregator()

        # See the matching block in ``pyfly.web.adapters.starlette.app``
        # for the timing rationale — beans are not yet instantiated at
        # ``create_app`` time, so we expose the scanner on ``app.state``
        # and let the downstream lifespan rerun it after startup.
        def _install_indicators() -> None:
            if context is None:
                return
            seen = set(agg._indicators.keys())  # noqa: SLF001
            for cls, reg in context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, HealthIndicator):
                    indicator_name = reg.name or cls.__name__
                    if indicator_name in seen:
                        continue
                    agg.add_indicator(indicator_name, reg.instance)
                    seen.add(indicator_name)

        _install_indicators()
        app.state.pyfly_install_health_indicators = _install_indicators
        _extra_post_start.append(_install_indicators)

        app.routes.extend(build_actuator_routes(context, agg, http_exchange_recorder))

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

        # Use the trace collector created above and wired into the filter chain.
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
        app.routes.extend(admin_builder.build_routes())

    # Register global exception handler
    register_exception_handlers(app)
    from pyfly.web.converters import build_exception_converter_service

    app.state.pyfly_exception_converter_service = build_exception_converter_service(context)

    # JSON serializer + HttpMessageConverter chain + RFC 7807 flag — same wiring as the
    # Starlette adapter (previously missing here, so content negotiation / global JSON
    # config / problem+json silently did not apply on the FastAPI adapter).
    from pyfly.web.message_converters import install_serialization_state

    install_serialization_state(app, context)

    # Collect route metadata (used for OpenAPI generation and startup logging)
    route_metadata = registrar.collect_route_metadata(context) if context is not None else []

    # Generate the OpenAPI spec ourselves. FastAPI's built-in introspection
    # cannot read PyFly's parameter-binding metadata (controller routes are
    # registered with include_in_schema=False), so we override ``app.openapi``
    # to serve the spec produced by ``OpenAPIGenerator`` from the collected
    # route metadata. FastAPI's ``/openapi.json``, ``/docs``, and ``/redoc``
    # routes then render from this spec — all guarded by ``docs_enabled`` via
    # the ``openapi_url``/``docs_url``/``redoc_url`` set on the constructor.
    if docs_enabled:
        generator = OpenAPIGenerator(title=title, version=version, description=description)
        spec = generator.generate(route_metadata or None)

        def _custom_openapi() -> dict[str, object]:
            app.openapi_schema = spec
            return spec

        app.openapi = _custom_openapi  # type: ignore[method-assign]

    # Store metadata for startup logging
    app.state.pyfly_route_metadata = route_metadata
    app.state.pyfly_docs_enabled = docs_enabled

    # Late beans (security/session WebFilters, @bean-produced controllers,
    # health indicators) are only instantiated by ApplicationContext.start(),
    # which runs inside the lifespan AFTER this factory returns. Re-run filter
    # discovery, controller-route registration, and the indicator scan once
    # startup completes (audit #40/#163).
    def _install_dynamic_wiring() -> None:
        _install_user_filters()
        _install_context_routes()
        for hook in _extra_post_start:
            hook()
        # Fold user @bean ExceptionConverter instances into the chain (audit #202).
        if context is not None:
            from pyfly.web.converters import build_exception_converter_service

            app.state.pyfly_exception_converter_service = build_exception_converter_service(context)

    app.state.pyfly_install_dynamic_wiring = _install_dynamic_wiring

    if context is not None and lifespan is not None:
        _inner_lifespan_ctx = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def _lifespan_with_dynamic_wiring(app_: FastAPI) -> AsyncIterator[None]:
            async with _inner_lifespan_ctx(app_):
                _install_dynamic_wiring()
                yield

        app.router.lifespan_context = _lifespan_with_dynamic_wiring

    return app
