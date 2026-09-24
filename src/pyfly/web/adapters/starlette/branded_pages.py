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
"""Shared branding context and packaged assets for browser pages."""

from __future__ import annotations

from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from pyfly.web.templating.config import BrandingProperties, WelcomeProperties
from pyfly.web.urls import reverse
from pyfly.web.views import ModelAndView


def web_asset_url(request: Request, path: str) -> str:
    return reverse(request, "pyfly_web_asset", path=path)


def branding_context(request: Request) -> dict[str, Any]:
    properties = getattr(request.app.state, "pyfly_branding", BrandingProperties())
    brand = asdict(properties)
    root = request.scope.get("root_path", "").rstrip("/")
    for key in ("logo_url", "favicon_url", "documentation_url", "support_url"):
        if brand[key].startswith("/"):
            brand[key] = root + brand[key]
    # Error rendering also works on manually constructed applications without
    # installed asset routes; these paths still provide useful branded markup.
    assets = root + properties.assets_path
    brand["logo_url"] = brand["logo_url"] or assets + "/logo.png"
    return {"branding": brand, "web_asset_url": partial(web_asset_url, request), "home_url": root + "/"}


async def web_asset(request: Request) -> Response:
    properties: BrandingProperties = request.app.state.pyfly_branding
    filename = request.path_params["path"]
    if filename == "theme.css":
        return Response(
            f":root{{--brand-primary:{properties.primary_color};--brand-accent:{properties.accent_color};}}",
            media_type="text/css",
            headers={"Cache-Control": "no-cache"},
        )
    if filename not in {"web.css", "logo.png"}:
        raise HTTPException(404)
    from pyfly.web import templating

    path = Path(templating.__file__).parent / "static" / filename
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


async def welcome_page(request: Request) -> Response:
    from pyfly.web.adapters.starlette.view_response import dispatch_response

    request.state.pyfly_view_controller = True
    properties: WelcomeProperties = request.app.state.pyfly_welcome
    return await dispatch_response(request, ModelAndView(properties.template))


def remove_welcome_fallback(app: Any) -> None:
    """Allow startup discovery to register a real home route before deduplication."""
    previous = getattr(app.state, "pyfly_welcome_route", None)
    if previous in app.router.routes:
        app.router.routes.remove(previous)


def install_branded_pages(app: Any, *, templates_enabled: bool, errors_enabled: bool) -> None:
    if not (templates_enabled or errors_enabled):
        return
    properties: BrandingProperties = app.state.pyfly_branding
    asset_route = getattr(app.state, "pyfly_brand_asset_route", None)
    if asset_route is None:
        asset_route = Route(properties.assets_path + "/{path:path}", web_asset, name="pyfly_web_asset")
    for existing in app.router.routes:
        if existing is asset_route:
            continue
        path = getattr(existing, "path", "")
        if (
            path == properties.assets_path
            or path.startswith(properties.assets_path + "/")
            or getattr(existing, "name", "") == "pyfly_web_asset"
        ):
            raise ValueError(f"Brand assets path/name conflicts with route: {path}")
    if asset_route not in app.router.routes:
        app.router.routes.append(asset_route)
    app.state.pyfly_brand_asset_route = asset_route
    remove_welcome_fallback(app)
    # Append the fallback after user routes on each startup, so late-discovered
    # controllers and catch-all application routers retain precedence.
    has_home = any(
        getattr(route, "path", "") in {"", "/"} and (getattr(route, "methods", None) is None or "GET" in route.methods)
        for route in app.router.routes
    )
    if templates_enabled and app.state.pyfly_welcome.enabled and not has_home:
        route = Route("/", welcome_page, name="pyfly_welcome")
        app.router.routes.append(route)
        app.state.pyfly_welcome_route = route
