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
"""Starlette adapter for actuator endpoints — generates routes from ActuatorRegistry."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint
from pyfly.actuator.endpoints.loggers_endpoint import LoggersEndpoint
from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint
from pyfly.actuator.registry import ActuatorRegistry


def make_starlette_actuator_routes(
    registry: ActuatorRegistry,
    exposed_ids: set[str] | None = None,
    base_path: str = "/actuator",
) -> list[Route]:
    """Build Starlette ``Route`` objects from all enabled endpoints in *registry*.

    *exposed_ids* — if provided, only endpoints whose id is in the set are mounted
    over HTTP (Spring Boot ``management.endpoints.web.exposure``). ``None`` exposes
    every enabled endpoint (used by low-level callers/tests).
    *base_path* — the actuator base path (Spring ``management.endpoints.web.base-path``).
    """
    enabled = registry.get_enabled_endpoints()
    if exposed_ids is not None:
        enabled = {eid: ep for eid, ep in enabled.items() if eid in exposed_ids}

    bp = base_path.rstrip("/")
    routes: list[Route] = []

    # Index endpoint: /actuator — lists all exposed endpoints with _links
    async def index_endpoint(request: Request) -> JSONResponse:
        links: dict[str, dict[str, str]] = {"self": {"href": bp or "/"}}
        for eid in enabled:
            links[eid] = {"href": f"{bp}/{eid}"}
        if "health" in enabled:
            links["health/liveness"] = {"href": f"{bp}/health/liveness"}
            links["health/readiness"] = {"href": f"{bp}/health/readiness"}
        return JSONResponse({"_links": links})

    routes.append(Route(bp or "/", index_endpoint, methods=["GET"]))

    for eid, ep in enabled.items():
        if isinstance(ep, HealthEndpoint):
            routes.extend(_make_health_routes(ep, bp))
        elif isinstance(ep, LoggersEndpoint):
            routes.extend(_make_loggers_routes(ep, bp))
        elif isinstance(ep, PrometheusEndpoint):
            routes.append(_make_prometheus_route(ep, bp))
        else:
            routes.extend(_make_generic_routes(eid, ep, bp))

    return routes


def _make_prometheus_route(ep: PrometheusEndpoint, bp: str) -> Route:
    """Prometheus scrape endpoint — must serve the raw text exposition format
    (``text/plain; version=0.0.4``), not a JSON wrapper."""

    async def handler(request: Request) -> Response:
        data = await ep.handle()
        return PlainTextResponse(
            data.get("body", ""),
            media_type=data.get("content_type") or "text/plain; version=0.0.4; charset=utf-8",
        )

    return Route(f"{bp}/prometheus", handler, methods=["GET"])


def _make_health_routes(ep: HealthEndpoint, bp: str) -> list[Route]:
    """Health endpoint returns dynamic status codes (200/503).

    Supports the built-in ``liveness``/``readiness`` probe groups plus a generic
    ``/health/{path}`` selector for any other group or single component.
    """

    async def handler(request: Request) -> JSONResponse:
        data = await ep.handle()
        status_code = await ep.get_status_code()
        return JSONResponse(data, status_code=status_code)

    async def liveness_handler(request: Request) -> JSONResponse:
        data = await ep.handle_liveness()
        status_code = await ep.get_liveness_status_code()
        return JSONResponse(data, status_code=status_code)

    async def readiness_handler(request: Request) -> JSONResponse:
        data = await ep.handle_readiness()
        status_code = await ep.get_readiness_status_code()
        return JSONResponse(data, status_code=status_code)

    async def selector_handler(request: Request) -> JSONResponse:
        # /actuator/health/{path} — a configured group, or a single component.
        path = request.path_params["path"]
        data, status_code = await ep.handle_path(path)
        if data is None:
            return JSONResponse({"error": f"No such health component or group: {path}"}, status_code=404)
        return JSONResponse(data, status_code=status_code)

    return [
        Route(f"{bp}/health", handler, methods=["GET"]),
        Route(f"{bp}/health/liveness", liveness_handler, methods=["GET"]),
        Route(f"{bp}/health/readiness", readiness_handler, methods=["GET"]),
        Route(f"{bp}/health/{{path:path}}", selector_handler, methods=["GET"]),
    ]


def _make_loggers_routes(ep: LoggersEndpoint, bp: str) -> list[Route]:
    """Loggers endpoint — Spring Boot shape.

    GET  /loggers            -> levels + loggers + groups
    GET  /loggers/{name}     -> {configuredLevel, effectiveLevel}
    POST /loggers/{name}     -> body {"configuredLevel": "DEBUG"} (or null to reset), 204
    """

    async def get_handler(request: Request) -> JSONResponse:
        data = await ep.handle()
        return JSONResponse(data)

    async def get_named_handler(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        return JSONResponse(await ep.get_logger(name))

    async def post_named_handler(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.body()
        payload = json.loads(body) if body else {}
        level = payload.get("configuredLevel")
        result = await ep.set_logger_level(name, level)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse(result, status_code=400)
        return Response(status_code=204)

    return [
        Route(f"{bp}/loggers", get_handler, methods=["GET"]),
        Route(f"{bp}/loggers/{{name}}", get_named_handler, methods=["GET"]),
        Route(f"{bp}/loggers/{{name}}", post_named_handler, methods=["POST"]),
    ]


def _make_generic_routes(eid: str, ep: object, bp: str) -> list[Route]:
    """Generic endpoint — ``GET /actuator/{id}`` plus an optional
    ``GET /actuator/{id}/{selector}`` drill-down when the endpoint opts in via
    ``supports_selector = True``."""

    async def handler(request: Request) -> JSONResponse:
        data = await ep.handle({"query": dict(request.query_params)})  # type: ignore[attr-defined]
        return JSONResponse(data)

    routes = [Route(f"{bp}/{eid}", handler, methods=["GET"])]

    if getattr(ep, "supports_selector", False):

        async def selector_handler(request: Request) -> JSONResponse:
            selector = request.path_params["selector"]
            data = await ep.handle(  # type: ignore[attr-defined]
                {"selector": selector, "query": dict(request.query_params)}
            )
            if data is None:
                return JSONResponse({"error": f"No such {eid}: {selector}"}, status_code=404)
            return JSONResponse(data)

        routes.append(Route(f"{bp}/{eid}/{{selector:path}}", selector_handler, methods=["GET"]))

    return routes
