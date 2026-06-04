# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Starlette HTTP adapter for :class:`ConfigServer`.

Exposes Spring-Cloud-Config-style routes so a config server (or pyfly's own
``ConfigClient``) can fetch ``/{application}/{profile}[/{label}]``. Starlette is
imported only here (hexagonal boundary)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from pyfly.config_server.server import ConfigServer


def make_starlette_config_server_routes(server: ConfigServer, base_path: str = "") -> list[Route]:
    """Build the config-server HTTP routes bound to *server*.

    - ``GET  {bp}/{application}/{profile}[/{label}]`` — fetch the merged config
    - ``POST {bp}/{application}/{profile}[/{label}]`` — save a config bundle
    - ``GET  {bp}/_list`` — list stored bundles
    """
    bp = base_path.rstrip("/")

    async def fetch_with_label(request: Request) -> JSONResponse:
        p = request.path_params
        payload = await server.fetch(p["application"], p["profile"], p["label"])
        if payload is None:
            return JSONResponse({"error": "configuration not found"}, status_code=404)
        return JSONResponse(payload)

    async def fetch(request: Request) -> JSONResponse:
        p = request.path_params
        payload = await server.fetch(p["application"], p["profile"])
        if payload is None:
            return JSONResponse({"error": "configuration not found"}, status_code=404)
        return JSONResponse(payload)

    async def save_with_label(request: Request) -> JSONResponse:
        p = request.path_params
        body = json.loads(await request.body() or b"{}")
        return JSONResponse(await server.save(p["application"], p["profile"], body, p["label"]))

    async def save(request: Request) -> JSONResponse:
        p = request.path_params
        body = json.loads(await request.body() or b"{}")
        return JSONResponse(await server.save(p["application"], p["profile"], body))

    async def list_sources(request: Request) -> JSONResponse:
        return JSONResponse(await server.list())

    return [
        Route(f"{bp}/_list", list_sources, methods=["GET"]),
        Route(f"{bp}/{{application}}/{{profile}}/{{label}}", fetch_with_label, methods=["GET"]),
        Route(f"{bp}/{{application}}/{{profile}}/{{label}}", save_with_label, methods=["POST"]),
        Route(f"{bp}/{{application}}/{{profile}}", fetch, methods=["GET"]),
        Route(f"{bp}/{{application}}/{{profile}}", save, methods=["POST"]),
    ]
