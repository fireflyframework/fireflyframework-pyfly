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
"""Builder for the separate management ASGI app (actuator + admin only).

When ``pyfly.management.server.port`` selects a separate port, the actuator
endpoints and the admin dashboard are served by this minimal Starlette app on the
management listener instead of the main business app. The data-capture filters
(metrics, http-exchange recorder, admin trace collector) stay on the *main* app so
the moved endpoints keep reporting on business traffic; this app gets only the
infrastructure + user security filters needed for the management endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route

from pyfly.container.ordering import get_order
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import (
    CorrelationFilter,
    RequestContextFilter,
    SecurityHeadersFilter,
    TracingFilter,
    TransactionIdFilter,
)
from pyfly.web.ports.filter import WebFilter

if TYPE_CHECKING:
    from pyfly.actuator.health import HealthAggregator
    from pyfly.actuator.http_exchanges import HttpExchangeRecorder
    from pyfly.context.application_context import ApplicationContext

# Capture filters belong on the MAIN app (they record business traffic). The
# management app must not re-add them or it would double-count and capture only
# management traffic.
_CAPTURE_FILTER_NAMES = frozenset(
    {"MetricsFilter", "HttpExchangeRecorderFilter", "TraceCollectorFilter", "RequestLoggingFilter"}
)


def create_management_app(
    context: ApplicationContext,
    *,
    health_agg: HealthAggregator | None,
    http_exchange_recorder: HttpExchangeRecorder | None,
    admin_trace_collector: Any | None,
    actuator_active: bool,
    admin_enabled: bool,
    base_path: str = "",
) -> Starlette:
    """Build the management-only Starlette app (no business routes).

    Called from the main app's lifespan AFTER ``ApplicationContext.start()`` so
    security WebFilter beans and health indicators already exist.
    """
    from pyfly.actuator.health import HealthAggregator
    from pyfly.actuator.wiring import build_actuator_routes, install_health_indicators

    filters: list[WebFilter] = [
        RequestContextFilter(),
        CorrelationFilter(),
        TracingFilter(),
        TransactionIdFilter(),
        SecurityHeadersFilter(),
    ]
    builtin_types = tuple(type(f) for f in filters)

    # Pull in user security/session/CSRF WebFilter beans so actuator/admin auth
    # works on the management port, excluding the capture filters owned by the
    # main app.
    present = {id(f) for f in filters}
    for _cls, reg in context.container._registrations.items():
        inst = reg.instance
        if (
            inst is not None
            and id(inst) not in present
            and isinstance(inst, WebFilter)
            and not isinstance(inst, builtin_types)
            and type(inst).__name__ not in _CAPTURE_FILTER_NAMES
        ):
            filters.append(inst)
            present.add(id(inst))
    filters.sort(key=lambda f: get_order(type(f)))

    middleware = [Middleware(WebFilterChainMiddleware, filters=filters)]

    routes: list[Route] = []
    agg = health_agg
    if actuator_active:
        if agg is None:
            agg = HealthAggregator()
            install_health_indicators(context, agg)
        routes.extend(build_actuator_routes(context, agg, http_exchange_recorder))

    if admin_enabled:
        from pyfly.admin.wiring import build_admin_routes

        routes.extend(
            build_admin_routes(
                context,
                admin_trace_collector=admin_trace_collector,
                base_health_agg=agg,
                extra_post_start=[],
            )
        )

    if base_path:
        # Mount everything under the configured base path (Spring base-path).
        inner = Starlette(routes=routes)
        return Starlette(middleware=middleware, routes=[Mount(base_path, app=inner)])
    return Starlette(middleware=middleware, routes=routes)
