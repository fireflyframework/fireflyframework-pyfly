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

    # Management server port separation (Spring management.server.* parity) — see
    # the matching block in pyfly.web.adapters.starlette.app for the rationale.
    management_mode = "shared"
    management_props = None
    if context is not None:
        import os as _os

        from pyfly.config.properties.server import resolve_app_port
        from pyfly.server.management_server import resolve_management_mode

        _env_port = _os.environ.get("_PYFLY_SERVER_PORT")
        _main_port = int(_env_port) if _env_port else resolve_app_port(context.config)
        management_mode, management_props = resolve_management_mode(context.config, _main_port)
    if management_mode == "disabled":
        actuator_active = False
        admin_enabled = False
    management_separated = management_mode == "separate"
    # Separation needs a lifespan hook to start the management listener; without
    # one, degrade to shared so actuator/admin are never silently unreachable.
    _serve_separately = management_separated and lifespan is not None

    # --- Build the WebFilter chain ---
    # RequestContextFilter runs first (HIGHEST_PRECEDENCE) so REQUEST-scoped beans
    # and @pre_authorize/@post_authorize have a live RequestContext to read.
    # Per-request access logging is on by default; disable to shave per-request footprint
    # (the structlog emit is the costliest filter) via pyfly.web.request-logging.enabled.
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
        from pyfly.actuator.health import HealthAggregator
        from pyfly.actuator.wiring import build_actuator_routes, install_health_indicators

        agg = HealthAggregator()

        # See the matching block in ``pyfly.web.adapters.starlette.app``
        # for the timing rationale — beans are not yet instantiated at
        # ``create_app`` time, so we expose the scanner on ``app.state``
        # and let the downstream lifespan rerun it after startup.
        def _install_indicators() -> None:
            install_health_indicators(context, agg)

        _install_indicators()
        app.state.pyfly_install_health_indicators = _install_indicators
        _extra_post_start.append(_install_indicators)

        # When separated, actuator routes live on the management app, not here.
        if not _serve_separately:
            app.routes.extend(build_actuator_routes(context, agg, http_exchange_recorder))

    # Mount admin dashboard when enabled (unless served on the management port).
    if admin_enabled and context is not None and not _serve_separately:
        from pyfly.admin.wiring import build_admin_routes

        app.routes.extend(
            build_admin_routes(
                context,
                admin_trace_collector=admin_trace_collector,
                base_health_agg=agg,
                extra_post_start=_extra_post_start,
            )
        )

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
                mgmt_server = None
                try:
                    if _serve_separately and management_props is not None and context is not None:
                        from pyfly.config.properties.server import resolve_app_host
                        from pyfly.server.management_server import ManagementServer
                        from pyfly.web.adapters.starlette.management_app import create_management_app

                        mgmt_app = create_management_app(
                            context,
                            health_agg=agg,
                            http_exchange_recorder=http_exchange_recorder,
                            admin_trace_collector=admin_trace_collector,
                            metrics_filter=metrics_filter_instance,
                            http_exchange_filter=http_exchange_filter,
                            actuator_active=actuator_active,
                            admin_enabled=admin_enabled,
                            base_path=management_props.base_path,
                        )
                        mgmt_host = management_props.address or resolve_app_host(context.config)
                        mgmt_server = ManagementServer(
                            mgmt_app, host=str(mgmt_host), port=int(management_props.port or 0)
                        )
                        await mgmt_server.start()
                    yield
                finally:
                    if mgmt_server is not None:
                        await mgmt_server.stop()

        app.router.lifespan_context = _lifespan_with_dynamic_wiring

    return app
