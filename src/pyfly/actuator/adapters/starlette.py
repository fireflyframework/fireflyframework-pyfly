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
from starlette.responses import JSONResponse
from starlette.routing import Route

from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint
from pyfly.actuator.endpoints.loggers_endpoint import LoggersEndpoint
from pyfly.actuator.registry import ActuatorRegistry


def make_starlette_actuator_routes(
    registry: ActuatorRegistry,
) -> list[Route]:
    """Build Starlette ``Route`` objects from all enabled endpoints in *registry*."""
    enabled = registry.get_enabled_endpoints()
    routes: list[Route] = []

    # Index endpoint: /actuator — lists all enabled endpoints with _links
    async def index_endpoint(request: Request) -> JSONResponse:
        links: dict[str, dict[str, str]] = {"self": {"href": "/actuator"}}
        for eid in enabled:
            links[eid] = {"href": f"/actuator/{eid}"}
        if "health" in enabled:
            links["health/liveness"] = {"href": "/actuator/health/liveness"}
            links["health/readiness"] = {"href": "/actuator/health/readiness"}
        return JSONResponse({"_links": links})

    routes.append(Route("/actuator", index_endpoint, methods=["GET"]))

    for eid, ep in enabled.items():
        if isinstance(ep, HealthEndpoint):
            routes.extend(_make_health_routes(ep))
        elif isinstance(ep, LoggersEndpoint):
            routes.extend(_make_loggers_routes(ep))
        else:
            routes.append(_make_generic_route(eid, ep))

    return routes


def _make_health_routes(ep: HealthEndpoint) -> list[Route]:
    """Health endpoint returns dynamic status codes (200/503)."""

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

    return [
        Route("/actuator/health", handler, methods=["GET"]),
        Route("/actuator/health/liveness", liveness_handler, methods=["GET"]),
        Route("/actuator/health/readiness", readiness_handler, methods=["GET"]),
    ]


def _make_loggers_routes(ep: LoggersEndpoint) -> list[Route]:
    """Loggers endpoint supports GET (list) and POST (change level)."""

    async def get_handler(request: Request) -> JSONResponse:
        data = await ep.handle()
        return JSONResponse(data)

    async def post_handler(request: Request) -> JSONResponse:
        body = await request.body()
        payload = json.loads(body) if body else {}
        logger_name = payload.get("logger", "ROOT")
        level = payload.get("level", "INFO")
        result = await ep.set_logger_level(logger_name, level)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    return [
        Route("/actuator/loggers", get_handler, methods=["GET"]),
        Route("/actuator/loggers", post_handler, methods=["POST"]),
    ]


def _make_generic_route(eid: str, ep: object) -> Route:
    """Generic endpoint — calls ``handle()`` and returns 200 JSON."""

    async def handler(request: Request) -> JSONResponse:
        data = await ep.handle()  # type: ignore[attr-defined]
        return JSONResponse(data)

    return Route(f"/actuator/{eid}", handler, methods=["GET"])
