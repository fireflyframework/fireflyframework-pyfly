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
"""Shared browser-application installation for both web adapters."""

from typing import Any

from starlette.exceptions import HTTPException

from pyfly.web.adapters.starlette.branded_pages import install_branded_pages
from pyfly.web.adapters.starlette.html_errors import http_exception_handler
from pyfly.web.adapters.starlette.static_resources import build_static_routes
from pyfly.web.templating.config import (
    BrandingProperties,
    HtmlErrorProperties,
    StaticProperties,
    TemplateProperties,
    WelcomeProperties,
)
from pyfly.web.templating.ports import TemplateContextProcessor, TemplateEngine


def install_webapp(app: Any, context: Any) -> None:
    if context is None:
        return
    config = context.config
    app.state.pyfly_html_errors = config.bind(HtmlErrorProperties)
    app.state.pyfly_config = config
    app.state.pyfly_branding = config.bind(BrandingProperties)
    app.state.pyfly_welcome = config.bind(WelcomeProperties)
    configured_debug = str(config.get("pyfly.web.debug", False)).lower() in {"true", "1", "yes", "on"}
    app.state.pyfly_web_debug = bool(getattr(app.state, "pyfly_web_debug", app.debug) or configured_debug)
    props = config.bind(TemplateProperties)
    if props.enabled:
        from pyfly.container.exceptions import NoSuchBeanError

        try:
            app.state.pyfly_template_engine = context.get_bean(TemplateEngine)
        except NoSuchBeanError:
            # Auto-configuration beans become available during the startup rescan.
            app.state.pyfly_template_engine = None
    app.state.pyfly_template_processors = [
        reg.instance
        for reg in context.container._registrations.values()
        if reg.instance is not None and isinstance(reg.instance, TemplateContextProcessor)
    ]
    if app.state.pyfly_html_errors.html_enabled:
        # Starlette's generic debug middleware bypasses registered error handlers.
        # PyFly owns negotiated diagnostics when HTML errors are enabled.
        app.debug = False
        app.add_exception_handler(HTTPException, http_exception_handler)
    static = config.bind(StaticProperties)
    if static.enabled:
        static_routes = getattr(app.state, "pyfly_static_routes", None)
        if static_routes is None:
            static_routes = build_static_routes(static)
        for route in static_routes:
            for existing in app.router.routes:
                if existing is route:
                    continue
                path = getattr(existing, "path", "")
                if path == route.path or path.startswith(route.path + "/"):
                    raise ValueError(f"Static path conflicts with route: {path}")
            if route not in app.router.routes:
                app.router.routes.append(route)
        app.state.pyfly_static_routes = static_routes
    install_branded_pages(app, templates_enabled=props.enabled, errors_enabled=app.state.pyfly_html_errors.html_enabled)
    names = [
        getattr(route, "name", None)
        for route in app.router.routes
        if getattr(getattr(route, "endpoint", None), "__pyfly_controller_route__", False)
    ]
    # Named PyFly controller routes must be unambiguous for URL reversal.
    names = [name for name in names if name and name != "lazy_endpoint"]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate route names")
