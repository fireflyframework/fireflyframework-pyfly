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
from collections.abc import AsyncIterator, Callable
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
    TracingFilter,
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
    # Per-request access logging is on by default; it can be disabled to shave per-request
    # footprint (the structlog emit is the costliest filter) via pyfly.web.request-logging.enabled.
    request_logging_enabled = context is None or str(
        context.config.get("pyfly.web.request-logging.enabled", "true")
    ).lower() in ("true", "1", "yes")
    filters: list[WebFilter] = [
        RequestContextFilter(),
        CorrelationFilter(),
        TracingFilter(),
        TransactionIdFilter(),
    ]
    if request_logging_enabled:
        filters.append(RequestLoggingFilter())
    filters.append(SecurityHeadersFilter())
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

    def _install_user_filters() -> int:
        """Append user ``WebFilter`` beans not already in the chain, re-sorting.

        Runs eagerly and again after ``ApplicationContext.start()`` so filters
        whose beans are only instantiated during startup (security, session,
        CSRF, HTTP-security) actually join the live chain (audit #40/#41/#42/#43).
        """
        if context is None:
            return 0
        present = {id(f) for f in filters}
        added = 0
        for _cls, reg in context.container._registrations.items():
            inst = reg.instance
            if (
                inst is not None
                and id(inst) not in present
                and isinstance(inst, WebFilter)
                and not isinstance(inst, builtin_filter_types)
                # The MetricsFilter is owned directly above; skip the bean copy
                # so the request timer is not wired (and counted) twice.
                and not (metrics_filter_type is not None and isinstance(inst, metrics_filter_type))
            ):
                filters.append(inst)
                present.add(id(inst))
                added += 1
        if added:
            # Built-in filters use HIGHEST_PRECEDENCE offsets; re-sort in place so
            # the live list (shared with WebFilterChainMiddleware) stays ordered.
            filters.sort(key=lambda f: get_order(type(f)))
        return added

    _install_user_filters()

    middleware: list[Middleware] = [
        Middleware(WebFilterChainMiddleware, filters=filters),
    ]

    # CORS auto-configuration (audit #204): when no explicit CORSConfig is passed,
    # build one from ``pyfly.web.cors.*`` so CORS is enabled purely via YAML, like
    # Spring's CorsAutoConfiguration. Secure-by-default: disabled unless opted in.
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

    routes: list[Route] = []
    registrar = ControllerRegistrar()

    def _collect_context_routes() -> list[Route]:
        """Collect controller / websocket / SSE / OAuth2-login routes.

        Re-runnable: @bean-produced controllers (orchestration, IDP, config
        server) only register their classes during ``ApplicationContext.start()``,
        so this is called eagerly and again after startup to mount routes that
        were not yet discoverable at ``create_app`` time (audit #163/#22/#44/#83).
        """
        if context is None:
            return []
        collected: list[Route] = list(registrar.collect_routes(context))
        collected.extend(WebSocketRegistrar().collect_routes(context))  # type: ignore[arg-type]

        from pyfly.web.sse.adapters.starlette import SSERegistrar

        collected.extend(SSERegistrar().collect_routes(context))

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
                    collected.extend(reg.instance.routes())
                    break
        return collected

    routes.extend(_collect_context_routes())

    # Config-server routes when the context is already started at create_app time
    # (otherwise the post-start rescan mounts them) — audit #83.
    if context is not None:
        from pyfly.config_server.wiring import build_config_server_routes

        routes.extend(build_config_server_routes(context))

    # Append caller-supplied routes (e.g. test helpers)
    if extra_routes:
        routes.extend(extra_routes)

    # Post-start hooks run once after ApplicationContext.start() (inside the
    # lifespan), when every bean is finally instantiated.
    _extra_post_start: list[Callable[[], None]] = []

    # Mount actuator endpoints when active (actuator_active resolved above).
    agg = None
    if actuator_active:
        from pyfly.actuator.health import HealthAggregator
        from pyfly.actuator.wiring import build_actuator_routes, install_health_indicators

        agg = HealthAggregator()

        # Auto-discover HealthIndicator beans from context.
        #
        # NOTE: ``create_app`` is typically called BEFORE the ApplicationContext
        # has started (the startup happens inside the ASGI ``lifespan``
        # function). At this point user / auto-configuration beans have
        # been *registered* but not *instantiated*, so the eager scan only
        # finds indicators that were attached as static singletons.
        #
        # The remaining indicators are picked up by ``_install_indicators``
        # which we attach to the Starlette ``on_startup`` hook below — by
        # the time on_startup fires, the lifespan has already triggered
        # ``PyFlyApplication.startup()`` and every bean has been built.
        def _install_indicators() -> None:
            install_health_indicators(context, agg)

        _install_indicators()
        _extra_post_start.append(_install_indicators)

        routes.extend(build_actuator_routes(context, agg, http_exchange_recorder))

    # Mount admin dashboard when enabled (admin_enabled computed above)
    if admin_enabled and context is not None:
        from pyfly.admin.wiring import build_admin_routes

        routes.extend(
            build_admin_routes(
                context,
                admin_trace_collector=admin_trace_collector,
                base_health_agg=agg,
                extra_post_start=_extra_post_start,
            )
        )

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

    # Late beans (security/session/CSRF WebFilters, @bean-produced controllers,
    # health indicators) are only instantiated by ``ApplicationContext.start()``,
    # which runs inside the provided lifespan — AFTER this factory returns. So
    # re-run filter discovery, controller-route collection, and the indicator
    # scan immediately after startup; otherwise authentication filters never run,
    # orchestration/IDP routes are unreachable, and /actuator/health reports UP
    # even when a subsystem is DOWN. (When no lifespan is supplied — e.g. tests
    # pass an already-started context — the eager scans above already caught
    # everything.)
    def _route_key(r: object) -> tuple[str, frozenset[str]]:
        return (getattr(r, "path", ""), frozenset(getattr(r, "methods", None) or ()))

    def _install_dynamic_wiring(app_: Starlette) -> None:
        _install_user_filters()
        existing = {_route_key(r) for r in app_.router.routes}
        for r in _collect_context_routes():
            key = _route_key(r)
            if key not in existing:
                app_.router.routes.append(r)
                existing.add(key)
        # Mount config-server routes — its ConfigServer bean only exists after
        # start(), so it must be discovered here, not at create_app time (#83).
        if context is not None:
            from pyfly.config_server.wiring import build_config_server_routes

            for r in build_config_server_routes(context):
                key = _route_key(r)
                if key not in existing:
                    app_.router.routes.append(r)
                    existing.add(key)
        for hook in _extra_post_start:
            hook()
        # Rebuild the exception-converter chain now that user @bean ExceptionConverter
        # instances exist (they are only created during start()) — audit #202.
        if context is not None:
            from pyfly.web.converters import build_exception_converter_service

            app_.state.pyfly_exception_converter_service = build_exception_converter_service(context)

    effective_lifespan = lifespan
    if context is not None and lifespan is not None:
        _user_lifespan = lifespan

        @contextlib.asynccontextmanager
        async def _lifespan_with_dynamic_wiring(app_: Starlette) -> AsyncIterator[None]:
            async with _user_lifespan(app_):  # type: ignore[operator]
                _install_dynamic_wiring(app_)  # beans are now instantiated
                yield

        effective_lifespan = _lifespan_with_dynamic_wiring

    app = Starlette(
        debug=debug,
        middleware=middleware,
        routes=routes,
        lifespan=effective_lifespan,  # type: ignore[arg-type]
    )

    # Store metadata for startup logging
    app.state.pyfly_route_metadata = route_metadata
    app.state.pyfly_docs_enabled = docs_enabled

    # Expose the post-start rescan for callers that manage their own lifespan.
    app.state.pyfly_install_dynamic_wiring = lambda: _install_dynamic_wiring(app)
    if actuator_active:
        app.state.pyfly_install_health_indicators = _install_indicators

    # Register global exception handler + its converter chain (audit #202).
    # Built-ins are available immediately; user converter beans are folded in by
    # _install_dynamic_wiring once the context has started.
    from pyfly.web.converters import build_exception_converter_service

    app.state.pyfly_exception_converter_service = build_exception_converter_service(context)
    app.add_exception_handler(Exception, global_exception_handler)

    # JSON serializer + HttpMessageConverter chain + RFC 7807 flag — shared with the
    # FastAPI adapter via install_serialization_state so the two cannot drift.
    from pyfly.web.message_converters import install_serialization_state

    install_serialization_state(app, context)

    return app
