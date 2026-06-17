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
"""Starlette adapter -- mounts admin dashboard routes and static files."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

if TYPE_CHECKING:
    from pyfly.admin.config import AdminProperties
    from pyfly.admin.middleware.trace_collector import TraceCollectorFilter
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
    from pyfly.admin.providers.observability_provider import ObservabilityProvider
    from pyfly.admin.providers.overview_provider import OverviewProvider
    from pyfly.admin.providers.runtime_provider import RuntimeProvider
    from pyfly.admin.providers.scheduled_provider import ScheduledProvider
    from pyfly.admin.providers.server_provider import ServerProvider
    from pyfly.admin.providers.traces_provider import TracesProvider
    from pyfly.admin.providers.transactions_provider import TransactionsProvider
    from pyfly.admin.registry import AdminViewRegistry
    from pyfly.admin.server.instance_registry import InstanceRegistry


_logger = logging.getLogger("pyfly.admin")


class _NoCacheStaticFiles(StaticFiles):
    """Serve admin assets with ``Cache-Control: no-cache`` so browsers revalidate.

    The dashboard's JS is loaded as ES modules whose relative imports carry no
    version query, so a plain far-future cache would serve stale views/styles
    after a framework upgrade. ``no-cache`` keeps revalidation cheap (304s) while
    guaranteeing updated assets are always picked up.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response: Response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class AdminRouteBuilder:
    """Builds Starlette routes for the admin dashboard."""

    def __init__(
        self,
        *,
        properties: AdminProperties,
        overview: OverviewProvider,
        beans: BeansProvider,
        health: HealthProvider,
        env: EnvProvider,
        config: ConfigProvider,
        loggers: LoggersProvider,
        metrics: MetricsProvider,
        scheduled: ScheduledProvider,
        mappings: MappingsProvider,
        caches: CacheProvider,
        cqrs: CqrsProvider,
        transactions: TransactionsProvider,
        traces: TracesProvider,
        view_registry: AdminViewRegistry,
        trace_collector: TraceCollectorFilter | None = None,
        logfile: LogfileProvider | None = None,
        runtime: RuntimeProvider | None = None,
        server: ServerProvider | None = None,
        observability: ObservabilityProvider | None = None,
        instance_registry: InstanceRegistry | None = None,
    ) -> None:
        self._props = properties
        self._overview = overview
        self._beans = beans
        self._health = health
        self._env = env
        self._config = config
        self._loggers = loggers
        self._metrics = metrics
        self._scheduled = scheduled
        self._mappings = mappings
        self._caches = caches
        self._cqrs = cqrs
        self._transactions = transactions
        self._traces = traces
        self._view_registry = view_registry
        self._trace_collector = trace_collector
        self._logfile = logfile
        self._runtime = runtime
        self._server = server
        self._observability = observability
        self._instance_registry = instance_registry

    def _auth_failure(self) -> JSONResponse | None:
        """Return a 401/403 response when require_auth is on and the caller lacks
        an allowed role; None when access is permitted (audit #66).

        Reads the request-scoped SecurityContext (populated by the security
        WebFilter chain). A no-op when pyfly.admin.require-auth is false.
        """
        if not self._props.require_auth:
            return None
        from pyfly.context.request_context import RequestContext

        rc = RequestContext.current()
        sec = rc.security_context if rc is not None else None
        if sec is None or not sec.is_authenticated:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if self._props.allowed_roles and not sec.has_any_role(self._props.allowed_roles):
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        return None

    def _guarded(self, handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        """Wrap an admin API handler so it enforces require_auth before running.

        Any unexpected error from the handler is converted to a structured JSON
        500 (and logged) instead of bubbling out as a raw Starlette 500. Admin
        introspection runs over a live container and can encounter unusual bean
        metadata; a single odd bean must never take down a whole dashboard view.
        """

        async def _wrapped(request: Request) -> Response:
            denied = self._auth_failure()
            if denied is not None:
                return denied
            try:
                return await handler(request)
            except Exception:
                _logger.exception("admin_api_handler_error path=%s", request.url.path)
                return JSONResponse(
                    {"error": "Internal admin error", "path": request.url.path},
                    status_code=500,
                )

        return _wrapped

    def build_routes(self) -> list[Route | Mount]:
        """Build all admin routes.

        Every ``{base}/api/*`` route (data, mutation, SSE, instance registry) is
        wrapped with the auth guard; the SPA shell + static assets stay public so
        the dashboard can boot and then surface 401s from the API (audit #66).
        """
        base = self._props.path.rstrip("/")
        api = f"{base}/api"

        routes: list[Route | Mount] = []

        # (path, handler, methods) for every guarded API + SSE route.
        guarded_specs: list[tuple[str, Any, list[str]]] = [
            (f"{api}/overview", self._handle_overview, ["GET"]),
            (f"{api}/beans", self._handle_beans, ["GET"]),
            (f"{api}/beans/graph", self._handle_bean_graph, ["GET"]),
            (f"{api}/beans/{{name}}", self._handle_bean_detail, ["GET"]),
            (f"{api}/health", self._handle_health, ["GET"]),
            (f"{api}/env", self._handle_env, ["GET"]),
            (f"{api}/config", self._handle_config, ["GET"]),
            (f"{api}/loggers", self._handle_loggers, ["GET"]),
            (f"{api}/loggers/{{name:path}}", self._handle_set_logger, ["POST"]),
            (f"{api}/metrics", self._handle_metrics, ["GET"]),
            (f"{api}/metrics/{{name:path}}", self._handle_metric_detail, ["GET"]),
            (f"{api}/scheduled", self._handle_scheduled, ["GET"]),
            (f"{api}/mappings", self._handle_mappings, ["GET"]),
            (f"{api}/caches", self._handle_caches, ["GET"]),
            (f"{api}/caches/keys", self._handle_cache_keys, ["GET"]),
            (f"{api}/caches/{{name}}/evict", self._handle_cache_evict, ["POST"]),
            (f"{api}/cqrs", self._handle_cqrs, ["GET"]),
            (f"{api}/transactions", self._handle_transactions, ["GET"]),
            (f"{api}/traces", self._handle_traces, ["GET"]),
            (f"{api}/logfile", self._handle_logfile, ["GET"]),
            (f"{api}/logfile/clear", self._handle_logfile_clear, ["POST"]),
            (f"{api}/runtime", self._handle_runtime, ["GET"]),
            (f"{api}/server", self._handle_server, ["GET"]),
            (f"{api}/observability", self._handle_observability, ["GET"]),
            (f"{api}/views", self._handle_views, ["GET"]),
            (f"{api}/settings", self._handle_settings, ["GET"]),
            (f"{api}/sse/health", self._handle_sse_health, ["GET"]),
            (f"{api}/sse/metrics", self._handle_sse_metrics, ["GET"]),
            (f"{api}/sse/traces", self._handle_sse_traces, ["GET"]),
            (f"{api}/sse/logfile", self._handle_sse_logfile, ["GET"]),
            (f"{api}/sse/runtime", self._handle_sse_runtime, ["GET"]),
            (f"{api}/sse/server", self._handle_sse_server, ["GET"]),
            (f"{api}/sse/observability", self._handle_sse_observability, ["GET"]),
            (f"{api}/sse/beans", self._handle_sse_beans, ["GET"]),
        ]

        # --- Instance registry routes (server mode) ---
        if self._instance_registry is not None:
            guarded_specs.extend(
                [
                    (f"{api}/instances", self._handle_instances_list, ["GET"]),
                    (f"{api}/instances", self._handle_instances_register, ["POST"]),
                    (f"{api}/instances/{{name}}", self._handle_instances_deregister, ["DELETE"]),
                ]
            )

        routes.extend(Route(path, self._guarded(handler), methods=methods) for path, handler, methods in guarded_specs)

        # --- Static files ---
        routes.append(
            Mount(
                f"{base}/static",
                app=_NoCacheStaticFiles(packages=[("pyfly.admin", "static")]),
                name="admin-static",
            )
        )

        # --- SPA catch-all (serves index.html for client-side routing) ---
        routes.append(Route(f"{base}/{{rest:path}}", self._handle_spa, methods=["GET"]))
        routes.append(Route(base, self._handle_spa, methods=["GET"]))

        return routes

    # --- API Handlers ---

    async def _handle_overview(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._overview.get_overview())

    async def _handle_beans(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._beans.get_beans())

    async def _handle_bean_graph(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._beans.get_bean_graph())

    async def _handle_bean_detail(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        detail = await self._beans.get_bean_detail(name)
        if detail is None:
            return JSONResponse({"error": "Bean not found"}, status_code=404)
        return JSONResponse(detail)

    async def _handle_health(self, request: Request) -> JSONResponse:
        data = await self._health.get_health()
        status_code = 503 if data.get("status") == "DOWN" else 200
        return JSONResponse(data, status_code=status_code)

    async def _handle_env(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._env.get_env())

    async def _handle_config(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._config.get_config())

    async def _handle_loggers(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._loggers.get_loggers())

    _VALID_LOG_LEVELS = frozenset(
        {
            "TRACE",
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
            "OFF",
        }
    )

    async def _handle_set_logger(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        body = await request.body()
        payload = json.loads(body) if body else {}
        level = payload.get("level", "INFO").upper()
        if level not in self._VALID_LOG_LEVELS:
            return JSONResponse(
                {"error": f"Invalid log level: {level}"},
                status_code=400,
            )
        result = await self._loggers.set_level(name, level)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    async def _handle_metrics(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._metrics.get_metric_names())

    async def _handle_metric_detail(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        return JSONResponse(await self._metrics.get_metric_detail(name))

    async def _handle_scheduled(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._scheduled.get_scheduled_tasks())

    async def _handle_mappings(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._mappings.get_mappings())

    async def _handle_caches(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._caches.get_caches())

    async def _handle_cache_keys(self, request: Request) -> JSONResponse:
        data = await self._caches.get_caches()
        return JSONResponse({"keys": data.get("keys", [])})

    async def _handle_cache_evict(self, request: Request) -> JSONResponse:
        _name = request.path_params["name"]
        body = await request.body()
        payload = json.loads(body) if body else {}
        key = payload.get("key")
        result = await self._caches.evict_cache(key)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    async def _handle_cqrs(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._cqrs.get_handlers())

    async def _handle_transactions(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._transactions.get_transactions())

    async def _handle_traces(self, request: Request) -> JSONResponse:
        limit = int(request.query_params.get("limit", "100"))
        return JSONResponse(await self._traces.get_traces(limit))

    async def _handle_logfile(self, request: Request) -> JSONResponse:
        if self._logfile is None:
            return JSONResponse({"available": False, "records": [], "total": 0})
        return JSONResponse(await self._logfile.get_logfile())

    async def _handle_logfile_clear(self, request: Request) -> JSONResponse:
        if self._logfile is None:
            return JSONResponse({"error": "Log handler not available"}, status_code=400)
        result = await self._logfile.clear_logfile()
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    async def _handle_runtime(self, request: Request) -> JSONResponse:
        if self._runtime is None:
            return JSONResponse({"available": False})
        return JSONResponse(await self._runtime.get_runtime())

    async def _handle_server(self, request: Request) -> JSONResponse:
        if self._server is None:
            return JSONResponse({"available": False})
        return JSONResponse(await self._server.get_server_info())

    async def _handle_observability(self, request: Request) -> JSONResponse:
        if self._observability is None:
            return JSONResponse({"available": False})
        return JSONResponse(await self._observability.get_observability())

    async def _handle_views(self, request: Request) -> JSONResponse:
        extensions = self._view_registry.get_extensions()
        views = [{"id": ext.view_id, "name": ext.display_name, "icon": ext.icon} for ext in extensions.values()]
        return JSONResponse({"views": views})

    async def _handle_settings(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "title": self._props.title,
                "theme": self._props.theme,
                "refreshInterval": self._props.refresh_interval,
                "serverMode": self._instance_registry is not None,
            }
        )

    # --- Instance Registry Handlers ---

    async def _handle_instances_list(self, request: Request) -> JSONResponse:
        assert self._instance_registry is not None
        return JSONResponse(self._instance_registry.to_dict())

    async def _handle_instances_register(self, request: Request) -> JSONResponse:
        assert self._instance_registry is not None
        body = await request.body()
        payload = json.loads(body) if body else {}
        name = payload.get("name", "")
        url = payload.get("url", "")
        if not name or not url:
            return JSONResponse({"error": "Both 'name' and 'url' are required"}, status_code=400)
        metadata = payload.get("metadata") or {}
        info = self._instance_registry.register(name, url, metadata)
        return JSONResponse(info.to_dict(), status_code=201)

    async def _handle_instances_deregister(self, request: Request) -> JSONResponse:
        assert self._instance_registry is not None
        name = request.path_params["name"]
        removed = self._instance_registry.deregister(name)
        if not removed:
            return JSONResponse({"error": "Instance not found"}, status_code=404)
        return JSONResponse({"removed": name})

    # --- SSE Handlers ---

    async def _handle_sse_health(self, request: Request) -> StreamingResponse:
        from pyfly.admin.api.sse import health_stream, make_sse_response

        interval = self._props.refresh_interval / 1000
        return make_sse_response(health_stream(self._health, interval))

    async def _handle_sse_metrics(self, request: Request) -> StreamingResponse:
        from pyfly.admin.api.sse import make_sse_response, metrics_stream

        interval = self._props.refresh_interval / 1000
        return make_sse_response(metrics_stream(self._metrics, interval))

    async def _handle_sse_traces(self, request: Request) -> StreamingResponse:
        from pyfly.admin.api.sse import make_sse_response, traces_stream

        return make_sse_response(traces_stream(self._trace_collector))

    async def _handle_sse_logfile(self, request: Request) -> StreamingResponse:
        from pyfly.admin.api.sse import logfile_stream, make_sse_response

        handler = self._logfile.handler if self._logfile is not None else None
        return make_sse_response(logfile_stream(handler))

    async def _handle_sse_runtime(self, request: Request) -> Response:
        from pyfly.admin.api.sse import make_sse_response, runtime_stream

        if self._runtime is None:
            return JSONResponse({"available": False})
        interval = self._props.refresh_interval / 1000
        return make_sse_response(runtime_stream(self._runtime, interval))

    async def _handle_sse_server(self, request: Request) -> Response:
        from pyfly.admin.api.sse import make_sse_response, server_stream

        if self._server is None:
            return JSONResponse({"available": False})
        interval = self._props.refresh_interval / 1000
        return make_sse_response(server_stream(self._server, interval))

    async def _handle_sse_observability(self, request: Request) -> Response:
        from pyfly.admin.api.sse import make_sse_response, observability_stream

        if self._observability is None:
            return JSONResponse({"available": False})
        interval = self._props.refresh_interval / 1000
        return make_sse_response(observability_stream(self._observability, interval))

    async def _handle_sse_beans(self, request: Request) -> Response:
        from pyfly.admin.api.sse import beans_stream, make_sse_response

        interval = self._props.refresh_interval / 1000
        return make_sse_response(beans_stream(self._beans, interval))

    async def _handle_spa(self, request: Request) -> Response:
        """Serve index.html for SPA client-side routing."""
        import importlib.resources
        import re

        from pyfly import __version__

        index_path = importlib.resources.files("pyfly.admin") / "static" / "index.html"
        content = index_path.read_text(encoding="utf-8")
        # Inject <base> so relative URLs (static/css/*, static/js/*) resolve
        # correctly regardless of whether the browser path has a trailing slash.
        base_href = self._props.path.rstrip("/") + "/"
        content = content.replace("<head>", f'<head>\n    <base href="{base_href}">', 1)
        # Version-stamp local static assets so a framework upgrade busts stale
        # browser caches (otherwise a cached themes.css/admin.css/app.js keeps the
        # old look after an upgrade).
        content = re.sub(
            r'(href|src)="(static/[^"?]+)"',
            rf'\1="\2?v={__version__}"',
            content,
        )
        # The SPA shell must revalidate so version-stamped asset URLs (?v=...)
        # are picked up after a framework upgrade. Without this, a heuristically
        # cached index.html keeps pointing at the previous version's assets.
        return Response(
            content,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )
