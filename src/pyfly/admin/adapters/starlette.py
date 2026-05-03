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
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

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
    from pyfly.admin.providers.overview_provider import OverviewProvider
    from pyfly.admin.providers.runtime_provider import RuntimeProvider
    from pyfly.admin.providers.scheduled_provider import ScheduledProvider
    from pyfly.admin.providers.server_provider import ServerProvider
    from pyfly.admin.providers.traces_provider import TracesProvider
    from pyfly.admin.providers.transactions_provider import TransactionsProvider
    from pyfly.admin.registry import AdminViewRegistry
    from pyfly.admin.server.instance_registry import InstanceRegistry


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
        self._instance_registry = instance_registry

    def build_routes(self) -> list[Route | Mount]:
        """Build all admin routes."""
        base = self._props.path.rstrip("/")
        api = f"{base}/api"

        routes: list[Route | Mount] = []

        # --- API routes ---
        routes.extend(
            [
                Route(f"{api}/overview", self._handle_overview, methods=["GET"]),
                Route(f"{api}/beans", self._handle_beans, methods=["GET"]),
                Route(f"{api}/beans/graph", self._handle_bean_graph, methods=["GET"]),
                Route(f"{api}/beans/{{name}}", self._handle_bean_detail, methods=["GET"]),
                Route(f"{api}/health", self._handle_health, methods=["GET"]),
                Route(f"{api}/env", self._handle_env, methods=["GET"]),
                Route(f"{api}/config", self._handle_config, methods=["GET"]),
                Route(f"{api}/loggers", self._handle_loggers, methods=["GET"]),
                Route(f"{api}/loggers/{{name:path}}", self._handle_set_logger, methods=["POST"]),
                Route(f"{api}/metrics", self._handle_metrics, methods=["GET"]),
                Route(f"{api}/metrics/{{name:path}}", self._handle_metric_detail, methods=["GET"]),
                Route(f"{api}/scheduled", self._handle_scheduled, methods=["GET"]),
                Route(f"{api}/mappings", self._handle_mappings, methods=["GET"]),
                Route(f"{api}/caches", self._handle_caches, methods=["GET"]),
                Route(f"{api}/caches/keys", self._handle_cache_keys, methods=["GET"]),
                Route(f"{api}/caches/{{name}}/evict", self._handle_cache_evict, methods=["POST"]),
                Route(f"{api}/cqrs", self._handle_cqrs, methods=["GET"]),
                Route(f"{api}/transactions", self._handle_transactions, methods=["GET"]),
                Route(f"{api}/traces", self._handle_traces, methods=["GET"]),
                Route(f"{api}/logfile", self._handle_logfile, methods=["GET"]),
                Route(f"{api}/logfile/clear", self._handle_logfile_clear, methods=["POST"]),
                Route(f"{api}/runtime", self._handle_runtime, methods=["GET"]),
                Route(f"{api}/server", self._handle_server, methods=["GET"]),
                Route(f"{api}/views", self._handle_views, methods=["GET"]),
                Route(f"{api}/settings", self._handle_settings, methods=["GET"]),
            ]
        )

        # --- SSE routes ---
        routes.extend(
            [
                Route(f"{api}/sse/health", self._handle_sse_health, methods=["GET"]),
                Route(f"{api}/sse/metrics", self._handle_sse_metrics, methods=["GET"]),
                Route(f"{api}/sse/traces", self._handle_sse_traces, methods=["GET"]),
                Route(f"{api}/sse/logfile", self._handle_sse_logfile, methods=["GET"]),
                Route(f"{api}/sse/runtime", self._handle_sse_runtime, methods=["GET"]),
                Route(f"{api}/sse/server", self._handle_sse_server, methods=["GET"]),
            ]
        )

        # --- Instance registry routes (server mode) ---
        if self._instance_registry is not None:
            routes.extend(
                [
                    Route(f"{api}/instances", self._handle_instances_list, methods=["GET"]),
                    Route(f"{api}/instances", self._handle_instances_register, methods=["POST"]),
                    Route(f"{api}/instances/{{name}}", self._handle_instances_deregister, methods=["DELETE"]),
                ]
            )

        # --- Static files ---
        routes.append(
            Mount(
                f"{base}/static",
                app=StaticFiles(packages=[("pyfly.admin", "static")]),
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

    async def _handle_spa(self, request: Request) -> Response:
        """Serve index.html for SPA client-side routing."""
        import importlib.resources

        index_path = importlib.resources.files("pyfly.admin") / "static" / "index.html"
        content = index_path.read_text(encoding="utf-8")
        # Inject <base> so relative URLs (static/css/*, static/js/*) resolve
        # correctly regardless of whether the browser path has a trailing slash.
        base_href = self._props.path.rstrip("/") + "/"
        content = content.replace("<head>", f'<head>\n    <base href="{base_href}">', 1)
        return Response(content, media_type="text/html")
