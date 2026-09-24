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
"""Negotiated HTML errors with a non-recursive escaped fallback."""

from __future__ import annotations

import html
import logging
import traceback
from functools import lru_cache, partial
from http import HTTPStatus
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from pyfly.web.adapters.starlette.branded_pages import branding_context
from pyfly.web.templating.config import HtmlErrorProperties
from pyfly.web.templating.ports import TemplateNotFoundError
from pyfly.web.urls import reverse, static_url

_logger = logging.getLogger(__name__)


def _trace_context(request: Request, exc: Exception | None, properties: HtmlErrorProperties) -> dict[str, Any]:
    enabled = properties.include_stacktrace == "always" or (
        properties.include_stacktrace == "on-debug" and bool(getattr(request.app.state, "pyfly_web_debug", False))
    )
    values: dict[str, Any] = {
        "show_stacktrace": enabled and exc is not None,
        "stack_trace": "",
        "stack_frames": [],
        "exception_type": "",
        "exception_message": "",
    }
    if not enabled or exc is None:
        return values
    frames = traceback.extract_tb(exc.__traceback__, limit=-properties.max_stack_frames)
    values["stack_frames"] = [
        {
            "filename": frame.filename[:500],
            "lineno": frame.lineno,
            "function": frame.name[:200],
            "source": (frame.line or "")[:500],
        }
        for frame in frames
    ]
    values["exception_type"] = type(exc).__name__
    values["exception_message"] = str(exc)[: properties.max_trace_length]
    lines = ["Traceback (most recent call last):\n"]
    for frame in values["stack_frames"]:
        lines.append(f'  File "{frame["filename"]}", line {frame["lineno"]}, in {frame["function"]}\n')
        if frame["source"]:
            lines.append(f"    {frame['source']}\n")
    lines.append(f"{values['exception_type']}: {values['exception_message']}")
    trace = "".join(lines)
    if len(trace) > properties.max_trace_length:
        trace = trace[: properties.max_trace_length - 20] + "\n… trace truncated"
    values["stack_trace"] = trace
    return values


@lru_cache(maxsize=1)
def _bundled_environment() -> Any:
    # A separate loader prevents a broken application base template from also
    # breaking the final error page. HTML errors remain usable without Jinja.
    from jinja2 import Environment, PackageLoader, StrictUndefined

    return Environment(
        loader=PackageLoader("pyfly.web.templating", "templates"),
        autoescape=True,
        enable_async=True,
        undefined=StrictUndefined,
    )


async def _fallback(values: dict[str, Any]) -> str:
    try:
        return str(await _bundled_environment().get_template("pyfly/error.html").render_async(values))
    except Exception:
        brand = values["branding"]
        assets = values["home_url"].rstrip("/") + brand["assets_path"]
        escape = html.escape
        trace = f"<pre>{escape(values['stack_trace'])}</pre>" if values["show_stacktrace"] else ""
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{values['status']} {escape(values['title'])} · {escape(brand['name'])}</title>"
            f'<link rel="stylesheet" href="{escape(assets)}/web.css">'
            f'<link rel="stylesheet" href="{escape(assets)}/theme.css"></head><body>'
            f'<div class="shell"><header><img class="brand-logo" src="{escape(brand["logo_url"])}" '
            f'alt="{escape(brand["name"])}"></header><main class="panel">'
            f"<h1>{values['status']} {escape(values['title'])}</h1><p>{escape(values['message'])}</p>"
            f'{trace}<a href="{escape(values["home_url"])}">Return home</a></main>'
            f'<footer>{escape(brand["footer"])} <a href="{escape(brand["support_url"])}">Support</a>'
            "</footer></div></body></html>"
        )


def _quality(accept: str, target: str) -> float:
    matches: list[tuple[int, float]] = []
    for item in accept.lower().split(","):
        media, *parameters = item.strip().split(";")
        specificity = 2 if media == target else 1 if media == target.split("/")[0] + "/*" else 0
        if not specificity and media != "*/*":
            continue
        q = 1.0
        for parameter in parameters:
            if parameter.strip().startswith("q="):
                try:
                    q = float(parameter.strip()[2:])
                    if not 0 <= q <= 1:
                        q = 0
                except ValueError:
                    q = 0
        matches.append((specificity, q))
    return max(matches)[1] if matches else 0


def wants_html(request: Request) -> bool:
    if getattr(request.state, "pyfly_rest_controller", False):
        return False
    accept = request.headers.get("accept", "")
    if "text/html" not in accept.lower():
        return bool(getattr(request.state, "pyfly_view_controller", False) and accept in ("", "*/*"))
    return _quality(accept, "text/html") > 0 and _quality(accept, "text/html") >= _quality(accept, "application/json")


async def render_html_error(
    request: Request,
    status: int,
    public_message: str,
    *,
    headers: dict[str, str] | None = None,
    exception: Exception | None = None,
) -> Response | None:
    if "app" not in request.scope:
        return None
    properties: HtmlErrorProperties = getattr(request.app.state, "pyfly_html_errors", HtmlErrorProperties())
    if not properties.html_enabled or not wants_html(request):
        return None
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Error"
    values: dict[str, Any] = {
        "status": status,
        "title": title,
        "message": public_message if status < 500 else title,
        "path": request.url.path,
        "transaction_id": getattr(request.state, "transaction_id", ""),
        "url_for": request.url_for,
        "reverse": partial(reverse, request),
        "static_url": partial(static_url, request),
    }
    values.update(branding_context(request))
    values.update(_trace_context(request, exception, properties))
    response_headers = dict(headers or {})
    response_headers.update({"Vary": "Accept", "Cache-Control": "no-store"})
    candidates = [
        properties.templates.get(str(status)),
        f"errors/{status}.html",
        f"errors/{status // 100}xx.html",
        properties.default_template,
    ]
    engine = getattr(request.app.state, "pyfly_template_engine", None)
    if engine is not None:
        for candidate in dict.fromkeys(candidates):
            if not candidate:
                continue
            try:
                body = await engine.render(candidate, values)
                return HTMLResponse(body, status_code=status, headers=response_headers)
            except TemplateNotFoundError:
                continue
            except Exception:
                _logger.exception("error_template_failed status=%s", status)
                break
    body = await _fallback(values)
    return HTMLResponse(body, status_code=status, headers=response_headers)


async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    rendered = await render_html_error(
        request, exc.status_code, str(exc.detail), headers=dict(exc.headers or {}), exception=exc
    )
    if rendered is not None:
        return rendered
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


async def render_filter_error(request: Request, response: Response) -> Response:
    """Negotiate security rejections before controller dispatch has run."""
    from starlette.routing import Match

    if "app" not in request.scope:
        return response
    properties = getattr(request.app.state, "pyfly_html_errors", None)
    if properties is None or not properties.html_enabled:
        return response
    routes = getattr(getattr(request.app, "router", None), "routes", [])
    scope = request.scope
    while routes:
        matched = False
        for route in routes:
            match, child = route.matches(scope)
            if match == Match.FULL:
                endpoint = child.get("endpoint")
                owner = getattr(endpoint, "__self__", None)
                if getattr(endpoint, "__pyfly_rest_controller__", False) or getattr(
                    owner, "__pyfly_rest_controller__", False
                ):
                    request.state.pyfly_rest_controller = True
                scope = {**scope, **child}
                routes = getattr(route, "routes", [])
                matched = True
                break
        if not matched:
            break
    rendered = await render_html_error(request, response.status_code, HTTPStatus(response.status_code).phrase)
    if rendered is None:
        return response
    excluded = {b"content-type", b"content-length", b"cache-control", b"vary"}
    rendered.raw_headers.extend((key, value) for key, value in response.raw_headers if key.lower() not in excluded)
    return rendered
