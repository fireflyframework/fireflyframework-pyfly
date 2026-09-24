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
"""Shared asynchronous response dispatch for the HTTP adapters."""

from __future__ import annotations

from functools import partial
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from pyfly.web.adapters.starlette.branded_pages import branding_context
from pyfly.web.adapters.starlette.response import handle_return_value
from pyfly.web.message_converters import MessageConverterRegistry
from pyfly.web.urls import reverse, static_url
from pyfly.web.views import ModelAndView, Redirect


async def dispatch_response(
    request: Request,
    result: Any,
    status_code: int = 200,
    *,
    converters: MessageConverterRegistry | None = None,
) -> Response:
    if isinstance(result, ModelAndView):
        engine = getattr(request.app.state, "pyfly_template_engine", None)
        if engine is None:
            raise RuntimeError("Configure pyfly.web.templates.enabled and a TemplateEngine to render views")
        context: dict[str, Any] = {}
        for processor in getattr(request.app.state, "pyfly_template_processors", ()):
            context.update(await processor.get_context(request))
        context.update(result.model)
        context.update(branding_context(request))
        context.update(
            url_for=request.url_for,
            reverse=partial(reverse, request),
            static_url=partial(static_url, request),
            csrf_token=getattr(request.state, "csrf_token", ""),
            csrf_field=getattr(request.state, "csrf_field", "_csrf"),
        )
        html = await engine.render(result.template, context)
        return HTMLResponse(html, status_code=result.status_code or status_code, headers=dict(result.headers))
    if isinstance(result, Redirect):
        return RedirectResponse(result.location, status_code=result.status_code)
    return handle_return_value(result, status_code, accept=request.headers.get("accept"), converters=converters)
