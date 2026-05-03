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
"""Swagger UI and ReDoc HTML endpoint handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - Swagger UI</title>
    <link rel="stylesheet" type="text/css"
          href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        SwaggerUIBundle({{
            url: "{openapi_url}",
            dom_id: '#swagger-ui',
            presets: [SwaggerUIBundle.presets.apis],
            layout: "BaseLayout",
            deepLinking: true,
            filter: true,
            persistAuthorization: true,
            showExtensions: true,
            showCommonExtensions: true,
            displayRequestDuration: true,
            docExpansion: "list",
            operationsSorter: "alpha",
            tagsSorter: "alpha",
            validatorUrl: null
        }})
    </script>
</body>
</html>"""

REDOC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - ReDoc</title>
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700"
          rel="stylesheet">
    <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
    <div id="redoc-container"></div>
    <script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
    <script>
        Redoc.init("{openapi_url}", {{
            expandResponses: "200,201",
            pathInMiddlePanel: true,
            nativeScrollbars: true,
            requiredPropsFirst: true,
            sortPropsAlphabetically: false,
            hideDownloadButton: false,
            theme: {{
                typography: {{
                    fontSize: '15px',
                    fontFamily: 'Roboto, sans-serif',
                    headings: {{ fontFamily: 'Montserrat, sans-serif' }}
                }},
                rightPanel: {{
                    backgroundColor: '#263238'
                }}
            }}
        }}, document.getElementById('redoc-container'))
    </script>
    <noscript>ReDoc requires JavaScript to render the API documentation.</noscript>
</body>
</html>"""


def make_openapi_endpoint(spec_dict: dict[str, Any]) -> Callable[[Request], Awaitable[JSONResponse]]:
    """Create the /openapi.json endpoint handler."""

    async def openapi_json(request: Request) -> JSONResponse:
        return JSONResponse(spec_dict)

    return openapi_json


def make_swagger_ui_endpoint(
    title: str,
    openapi_url: str = "/openapi.json",
) -> Callable[[Request], Awaitable[HTMLResponse]]:
    """Create the /docs endpoint handler."""

    async def swagger_ui(request: Request) -> HTMLResponse:
        html = SWAGGER_UI_HTML.format(title=title, openapi_url=openapi_url)
        return HTMLResponse(html)

    return swagger_ui


def make_redoc_endpoint(
    title: str,
    openapi_url: str = "/openapi.json",
) -> Callable[[Request], Awaitable[HTMLResponse]]:
    """Create the /redoc endpoint handler."""

    async def redoc(request: Request) -> HTMLResponse:
        html = REDOC_HTML.format(title=title, openapi_url=openapi_url)
        return HTMLResponse(html)

    return redoc
