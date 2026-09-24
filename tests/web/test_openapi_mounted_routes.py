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
"""Routes handed to ``create_app(extra_routes=...)`` reach the OpenAPI document.

A service that mounts provider webhooks as Starlette sub-applications (``Mount("/api/slack",
app=...)``) beside its ``@rest_controller`` routes had an ``/openapi.json`` that described the
controllers and nothing else: the paths carrying the traffic were invisible, so a CI diff of the
document could not notice one of them disappearing. The generator worked from controller
metadata only. It now also walks the extra routes — plain ``Route`` objects and the routes inside
a ``Mount`` whose app is a router, recursively, with the mount prefix — and emits them as
operations marked ``x-pyfly-mounted: true``, since they carry no typed contract. A mount whose
app cannot be walked (static files, a foreign ASGI app) is listed under ``x-pyfly-mounts`` so
the document at least says the prefix exists.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.mounted_routes import MountedParameter, MountedRoute, collect_mounted_routes
from pyfly.web.openapi import OpenAPIGenerator


async def events(request: Request) -> JSONResponse:
    """Receive a Slack event callback."""
    return JSONResponse({"ok": True})


async def commands(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def ping(request: Request) -> PlainTextResponse:
    """Liveness for the relay.

    A second paragraph the summary must not include.
    """
    return PlainTextResponse("pong")


async def opaque(scope, receive, send):  # type: ignore[no-untyped-def]
    pass


async def updates(request: Request) -> JSONResponse:
    """Receive a Telegram update."""
    return JSONResponse({"ok": True})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "UP"})


def _slack() -> Starlette:
    return Starlette(
        routes=[
            Route("/events", events, methods=["POST"], name="slack.events"),
            Route("/commands", commands, methods=["POST", "GET"]),
            Mount("/v2", app=Starlette(routes=[Route("/events", events, methods=["POST"])])),
        ]
    )


class TestCollectMountedRoutes:
    def test_walks_routes_and_mounts_with_their_prefix(self) -> None:
        found = collect_mounted_routes(
            [
                Route("/ping", ping, methods=["GET"]),
                Mount("/api/slack", app=_slack()),
                Mount("/static", app=opaque),
            ]
        )
        by_path = {(r.path, r.method): r for r in found}
        assert set(by_path) == {
            ("/ping", "GET"),
            ("/api/slack/events", "POST"),
            ("/api/slack/commands", "POST"),
            ("/api/slack/commands", "GET"),
            ("/api/slack/v2/events", "POST"),
            ("/static", None),
        }
        assert by_path[("/api/slack/events", "POST")].name == "slack.events"
        assert by_path[("/api/slack/events", "POST")].summary == "Receive a Slack event callback."
        assert by_path[("/api/slack/commands", "GET")].name == "commands"
        assert by_path[("/ping", "GET")].summary == "Liveness for the relay."
        # Starlette adds HEAD to every GET route; that is transport, not contract.
        assert not any(r.method == "HEAD" for r in found)

    def test_path_parameters_are_read_off_the_route_and_the_mount(self) -> None:
        """A path template such as ``/{botId}/updates`` names a parameter the document MUST
        declare (OpenAPI 3.1: every template expression has a ``parameters`` entry). Starlette
        keeps the convertor on the route; the mount prefix may carry parameters too."""
        found = collect_mounted_routes(
            [
                Mount(
                    "/api/telegram",
                    app=Starlette(routes=[Route("/{botId}/updates", updates, methods=["POST"])]),
                ),
                Mount(
                    "/tenants/{tenant:uuid}",
                    app=Starlette(routes=[Route("/files/{name:path}", ping, methods=["GET"])]),
                ),
                Route("/items/{item_id:int}/price/{amount:float}", ping, methods=["GET"]),
            ]
        )
        by_path = {r.path: r for r in found}
        assert by_path["/api/telegram/{botId}/updates"].parameters == (MountedParameter("botId", "string", None),)
        # The convertor suffix is Starlette syntax, not OpenAPI: the template keeps only the name.
        assert "/tenants/{tenant}/files/{name}" in by_path
        assert by_path["/tenants/{tenant}/files/{name}"].parameters == (
            MountedParameter("tenant", "string", "uuid"),
            MountedParameter("name", "string", None),
        )
        assert by_path["/items/{item_id}/price/{amount}"].parameters == (
            MountedParameter("item_id", "integer", None),
            MountedParameter("amount", "number", None),
        )

    def test_a_mount_whose_app_cannot_be_walked_is_an_opaque_prefix(self) -> None:
        (found,) = collect_mounted_routes([Mount("/static", app=opaque, name="static")])
        assert found == MountedRoute(path="/static", method=None, name="static", summary="")


class TestGeneratorEmitsMountedRoutes:
    def test_mounted_routes_become_marked_operations(self) -> None:
        spec = OpenAPIGenerator(title="t", version="1").generate(
            mounted_routes=[
                MountedRoute("/api/slack/events", "POST", "slack.events", "Receive a Slack event callback."),
                MountedRoute("/ping", "GET", "ping", ""),
                MountedRoute("/static", None, "static", ""),
            ]
        )
        assert spec["paths"]["/api/slack/events"]["post"] == {
            "operationId": "slack.events",
            "summary": "Receive a Slack event callback.",
            "responses": {"default": {"description": "Successful response"}},
            "x-pyfly-mounted": True,
        }
        assert spec["paths"]["/ping"]["get"]["operationId"] == "ping"
        assert "summary" not in spec["paths"]["/ping"]["get"]
        assert spec["x-pyfly-mounts"] == [{"path": "/static", "name": "static"}]
        assert "/static" not in spec["paths"]

    def test_path_parameters_are_declared_on_the_operation(self) -> None:
        spec = OpenAPIGenerator(title="t", version="1").generate(
            mounted_routes=[
                MountedRoute(
                    "/api/telegram/{botId}/updates",
                    "POST",
                    "updates",
                    "",
                    parameters=(MountedParameter("botId", "string", None), MountedParameter("n", "integer", None)),
                ),
                MountedRoute(
                    "/tenants/{tenant}", "GET", "tenant", "", parameters=(MountedParameter("tenant", "string", "uuid"),)
                ),
            ]
        )
        assert spec["paths"]["/api/telegram/{botId}/updates"]["post"]["parameters"] == [
            {"name": "botId", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "n", "in": "path", "required": True, "schema": {"type": "integer"}},
        ]
        assert spec["paths"]["/tenants/{tenant}"]["get"]["parameters"] == [
            {"name": "tenant", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}},
        ]

    def test_operation_ids_are_unique_across_the_document(self) -> None:
        """Six sub-apps each with a ``health`` endpoint produced six ``operationId: health`` —
        an OpenAPI document that does not validate. The first keeps the bare name; the others
        are qualified by method and path, deterministically, so a diff of the document is stable."""
        from pyfly.web.adapters.starlette.controller import RouteMetadata

        meta = RouteMetadata(path="/status", http_method="GET", status_code=200, handler=ping, handler_name="health")
        spec = OpenAPIGenerator(title="t", version="1").generate(
            route_metadata=[meta],
            mounted_routes=[
                MountedRoute("/api/slack/health", "GET", "health", ""),
                MountedRoute("/api/teams/health", "GET", "health", ""),
                MountedRoute(
                    "/api/telegram/{botId}/health",
                    "GET",
                    "health",
                    "",
                    parameters=(MountedParameter("botId", "string", None),),
                ),
                MountedRoute("/api/events", "POST", "events", ""),
                MountedRoute("/api/events", "GET", "events", ""),
            ],
        )
        ids = [op["operationId"] for path in spec["paths"].values() for op in path.values()]
        assert len(ids) == len(set(ids)), ids
        # The controller's id is untouched; the mounted twins are qualified.
        assert spec["paths"]["/status"]["get"]["operationId"] == "health"
        assert spec["paths"]["/api/slack/health"]["get"]["operationId"] == "health_get_api_slack_health"
        assert spec["paths"]["/api/teams/health"]["get"]["operationId"] == "health_get_api_teams_health"
        assert (
            spec["paths"]["/api/telegram/{botId}/health"]["get"]["operationId"]
            == "health_get_api_telegram_botId_health"
        )
        assert spec["paths"]["/api/events"]["post"]["operationId"] == "events"
        assert spec["paths"]["/api/events"]["get"]["operationId"] == "events_get_api_events"

    def test_a_controller_operation_is_never_overwritten_by_a_mounted_one(self) -> None:
        from pyfly.web.adapters.starlette.controller import RouteMetadata

        meta = RouteMetadata(path="/ping", http_method="GET", status_code=200, handler=ping, handler_name="typed_ping")
        spec = OpenAPIGenerator(title="t", version="1").generate(
            route_metadata=[meta],
            mounted_routes=[MountedRoute("/ping", "GET", "ping", "")],
        )
        assert spec["paths"]["/ping"]["get"]["operationId"] == "typed_ping"
        assert "x-pyfly-mounted" not in spec["paths"]["/ping"]["get"]


class TestCreateAppDocument:
    def test_openapi_json_lists_the_extra_routes(self) -> None:
        app = create_app(
            docs_enabled=True,
            extra_routes=[Route("/ping", ping, methods=["GET"]), Mount("/api/slack", app=_slack())],
        )
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        assert set(spec["paths"]) >= {"/ping", "/api/slack/events", "/api/slack/commands", "/api/slack/v2/events"}
        assert spec["paths"]["/api/slack/events"]["post"]["x-pyfly-mounted"] is True
        # ``/api/slack/v2/events`` reuses the ``events`` endpoint: the document still validates.
        ids = [op["operationId"] for path in spec["paths"].values() for op in path.values()]
        assert len(ids) == len(set(ids)), ids
        # The document does not describe itself or the doc pages.
        assert not {"/openapi.json", "/docs", "/redoc"} & set(spec["paths"])
        # And the routes really are served.
        assert client.post("/api/slack/events").status_code == 200
        assert client.get("/ping").text == "pong"

    def test_openapi_json_declares_the_parameters_of_a_mounted_template(self) -> None:
        app = create_app(
            docs_enabled=True,
            extra_routes=[
                Mount("/api/telegram", app=Starlette(routes=[Route("/{botId}/updates", updates, methods=["POST"])])),
            ],
        )
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        operation = spec["paths"]["/api/telegram/{botId}/updates"]["post"]
        assert operation["parameters"] == [
            {"name": "botId", "in": "path", "required": True, "schema": {"type": "string"}},
        ]
        assert client.post("/api/telegram/bot-1/updates").status_code == 200
