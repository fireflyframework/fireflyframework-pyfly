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
"""Controller discovery, route collection, and request dispatching."""

from __future__ import annotations

import inspect
import typing
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.adapters.starlette.view_response import dispatch_response
from pyfly.web.openapi_metadata import RouteMetadata as RouteMetadata
from pyfly.web.params import Body, Cookie, Header, PathVar, QueryParam, _Binding, inspect_binding

_MISSING = object()


def _py_type_to_openapi(t: type) -> str:
    """Map a Python type to an OpenAPI schema type string."""
    if t is int:
        return "integer"
    if t is float:
        return "number"
    if t is bool:
        return "boolean"
    return "string"


def _static_handler(cls: type, name: str) -> Any:
    """Read only ordinary/static/class methods; never execute user descriptors."""
    value = inspect.getattr_static(cls, name, None)
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    return value if inspect.isfunction(value) else None


def _schema_type(hint: Any) -> Any:
    """Remove binding sentinels while preserving Pydantic's Annotated constraints."""
    metadata = getattr(hint, "__metadata__", ())
    if not metadata:
        return hint
    inner = typing.get_args(hint)[0]
    constraints = tuple(item for item in metadata if not isinstance(item, _Binding))
    return typing.Annotated[(inner, *constraints)] if constraints else inner


async def _maybe_await(result: Any) -> Any:
    """Await the result if it's a coroutine, otherwise return as-is."""
    if inspect.isawaitable(result):
        return await result
    return result


class ControllerRegistrar:
    """Discovers ``@rest_controller`` and ``@controller`` beans and builds Starlette routes.

    For each controller:
    1. Reads @request_mapping base path from the class
    2. Finds @*_mapping handler methods
    3. Builds a ParameterResolver for each handler
    4. Collects @exception_handler methods
    5. Creates Starlette Route objects that dispatch requests

    Also collects ``@controller_advice`` beans for global exception handling.
    """

    _CONTROLLER_STEREOTYPES = ("rest_controller", "controller")

    def __init__(self) -> None:
        self._global_exception_handlers: dict[type[Exception], Any] | None = None

    def collect_routes(self, ctx: Any) -> list[Route]:
        """Collect all routes from ``@rest_controller`` and ``@controller`` beans.

        Bean resolution is deferred until the first HTTP request hits each
        controller, avoiding eager resolution of the full dependency tree
        during ``create_app()`` (before auto-configurations have run).
        """
        routes: list[Route] = []

        for cls, _reg in ctx.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") not in self._CONTROLLER_STEREOTYPES:
                continue

            base_path = getattr(cls, "__pyfly_request_mapping__", "")

            for attr_name in dir(cls):
                method_obj = _static_handler(cls, attr_name)
                if method_obj is None:
                    continue

                mapping = getattr(method_obj, "__pyfly_mapping__", None)
                if mapping is None:
                    continue

                full_path = base_path + mapping["path"]
                http_method = mapping["method"]
                status_code = mapping.get("status_code", 200)

                handler = self._make_lazy_handler(ctx, cls, attr_name, status_code)
                routes.append(
                    Route(
                        full_path,
                        handler,
                        methods=[http_method],
                        name=mapping.get("name") or f"{cls.__module__}.{cls.__qualname__}.{attr_name}",
                    )
                )

        return routes

    def collect_route_metadata(self, ctx: Any) -> list[RouteMetadata]:
        """Collect route metadata from ``@rest_controller`` and ``@controller`` classes.

        All OpenAPI metadata (type hints, mappings, docstrings) lives on the
        class — no bean resolution needed.
        """
        metadata: list[RouteMetadata] = []

        for cls, _reg in ctx.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") not in self._CONTROLLER_STEREOTYPES:
                continue

            base_path = getattr(cls, "__pyfly_request_mapping__", "")
            tag = self._derive_tag(cls)

            for attr_name in dir(cls):
                method_obj = _static_handler(cls, attr_name)
                if method_obj is None:
                    continue

                mapping = getattr(method_obj, "__pyfly_mapping__", None)
                sse_mapping = getattr(method_obj, "__pyfly_sse_mapping__", None)

                if mapping is None and sse_mapping is None:
                    # A @websocket_mapping lands here. WebSocket is a different protocol with no
                    # OpenAPI representation, so it is deliberately not an operation — but the
                    # omission is no longer silent: collect_websocket_routes() reports those routes
                    # and the generator publishes them as x-pyfly-websocket-routes.
                    continue

                if mapping is not None:
                    full_path = base_path + mapping["path"]
                    http_method = mapping["method"]
                    status_code = mapping.get("status_code", 200)
                    media_type = "application/json"
                elif sse_mapping is not None:
                    # Server-sent events are a GET that does not close. Describing it as one is what
                    # lets a CI job export /openapi.json and diff the WHOLE surface rather than only
                    # its request/response half.
                    full_path = base_path + sse_mapping["path"]
                    http_method = "GET"
                    status_code = 200
                    media_type = "text/event-stream"

                # Extract parameter metadata and request body model from type hints
                params, body_model = self._extract_param_metadata(method_obj)
                hints = typing.get_type_hints(method_obj, include_extras=True)
                parameter_types = {}
                for name, hint in hints.items():
                    binding, _inner, _valid = inspect_binding(hint)
                    location = {PathVar: "path", QueryParam: "query", Header: "header", Cookie: "cookie"}.get(binding)
                    if location is not None:
                        wire_name = name.replace("_", "-") if binding is Header else name
                        parameter_types[location, wire_name] = _schema_type(hint)

                # Extract return type
                return_type = hints.get("return")
                from pyfly.web.views import ModelAndView

                if return_type is ModelAndView:
                    media_type = "text/html"

                # Extract summary and description from docstring
                summary, description = self._parse_docstring(method_obj)

                # Check deprecated flag
                deprecated = getattr(method_obj, "__pyfly_deprecated__", False)

                metadata.append(
                    RouteMetadata(
                        path=full_path,
                        http_method=http_method,
                        status_code=status_code,
                        handler=method_obj,
                        handler_name=attr_name,
                        parameters=params,
                        request_body_model=body_model,
                        return_type=return_type,
                        tag=tag,
                        summary=summary,
                        description=description,
                        deprecated=deprecated,
                        media_type=media_type,
                        mapping_name=mapping.get("name") if mapping is not None else None,
                        operation=getattr(method_obj, "__pyfly_openapi__", None),
                        parameter_types=parameter_types,
                    )
                )

        return metadata

    def collect_websocket_routes(self, ctx: Any) -> list[dict[str, str]]:
        """The ``@websocket_mapping`` routes of every controller, in declaration order.

        WebSocket is not expressible in OpenAPI — that is what AsyncAPI is for — so these are
        deliberately not operations. They were also simply absent from the generated document with
        nothing said about them, which meant a service could delete a socket route and an OpenAPI diff
        would report no change at all. Returning them here lets the generator publish them under the
        document-level ``x-pyfly-websocket-routes`` extension: still not an operation, but visible,
        diffable, and honest about what the document does not cover.
        """
        routes: list[dict[str, str]] = []

        for cls, _reg in ctx.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") not in self._CONTROLLER_STEREOTYPES:
                continue

            base_path = getattr(cls, "__pyfly_request_mapping__", "")

            for attr_name in dir(cls):
                method_obj = _static_handler(cls, attr_name)
                ws_mapping = getattr(method_obj, "__pyfly_ws_mapping__", None) if method_obj else None

                if ws_mapping is None:
                    continue

                summary, _description = self._parse_docstring(method_obj)
                routes.append(
                    {
                        "path": base_path + ws_mapping["path"],
                        "handler": attr_name,
                        "summary": summary,
                    }
                )

        return routes

    @staticmethod
    def _derive_tag(cls: type) -> str:
        """Derive an OpenAPI tag from the controller class name.

        ``CatalogController`` → ``Catalog``, ``HealthController`` → ``Health``.
        """
        name = cls.__name__
        if name.endswith("Controller"):
            name = name[: -len("Controller")]
        return name

    @staticmethod
    def _parse_docstring(handler: Any) -> tuple[str, str]:
        """Extract summary and description from handler docstring.

        First non-empty line → summary.
        Remaining lines (after a blank line separator) → description.
        """
        doc = inspect.getdoc(handler)
        if not doc:
            return "", ""
        lines = doc.strip().splitlines()
        summary = lines[0].strip()
        description = ""
        if len(lines) > 2 and not lines[1].strip():
            description = "\n".join(line.strip() for line in lines[2:]).strip()
        return summary, description

    def _extract_param_metadata(self, handler: Any) -> tuple[list[dict[str, Any]], Any]:
        """Keep legacy parameter dictionaries alongside rich types on RouteMetadata."""
        hints = typing.get_type_hints(handler, include_extras=True)
        params: list[dict[str, Any]] = []
        body_model: Any = None
        for name, param in inspect.signature(handler).parameters.items():
            hint = hints.get(name)
            if hint is None:
                continue
            binding, inner_type, _validate = inspect_binding(hint)
            if binding is Body:
                body_model = _schema_type(hint)
                continue
            location = {PathVar: "path", QueryParam: "query", Header: "header", Cookie: "cookie"}.get(binding)
            if location is None:
                continue
            default = param.default if param.default is not inspect.Parameter.empty else _MISSING
            required = binding is PathVar or (default is _MISSING and type(None) not in typing.get_args(inner_type))
            item: dict[str, Any] = {
                "name": name.replace("_", "-") if binding is Header else name,
                "in": location,
                "required": required,
                "schema": {"type": _py_type_to_openapi(inner_type)},
            }
            if default is not _MISSING:
                item["schema"]["default"] = default
            params.append(item)
        return params, body_model

    def _collect_exception_handlers(self, instance: Any) -> dict[type[Exception], Any]:
        """Collect all @exception_handler methods from a controller instance.

        Handlers are sorted by MRO depth (most specific first) so subclass
        exceptions are matched before their parents.
        """
        handlers: dict[type[Exception], Any] = {}
        for attr_name in dir(instance):
            method = getattr(instance, attr_name, None)
            if method is None:
                continue
            exc_type = getattr(method, "__pyfly_exception_handler__", None)
            if exc_type is not None:
                handlers[exc_type] = method
        return dict(sorted(handlers.items(), key=lambda item: len(item[0].__mro__), reverse=True))

    def _collect_global_advice_handlers(self, ctx: Any) -> dict[type[Exception], Any]:
        """Collect @exception_handler methods from all @controller_advice beans.

        Handlers are sorted by MRO depth (most specific first). Controller-local
        handlers always take priority over global advice.
        """
        handlers: dict[type[Exception], Any] = {}
        for cls, reg in ctx.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") != "controller_advice":
                continue
            instance = reg.instance
            if instance is None:
                instance = ctx.get_bean(cls)
            handlers.update(self._collect_exception_handlers(instance))
        return dict(sorted(handlers.items(), key=lambda item: len(item[0].__mro__), reverse=True))

    def _get_global_advice_handlers(self, ctx: Any) -> dict[type[Exception], Any]:
        """Return cached global advice handlers, collecting on first call."""
        if self._global_exception_handlers is None:
            self._global_exception_handlers = self._collect_global_advice_handlers(ctx)
        return self._global_exception_handlers

    def _make_lazy_handler(
        self,
        ctx: Any,
        controller_cls: type,
        method_name: str,
        status_code: int,
    ) -> Any:
        """Create a Starlette endpoint that lazily resolves the controller bean on first request."""
        import asyncio

        _cache: dict[str, Any] = {}
        _init_lock = asyncio.Lock()

        async def lazy_endpoint(request: Request) -> Response:
            instance = ctx.get_bean(controller_cls)
            if _cache.get("instance") is not instance:
                async with _init_lock:
                    if _cache.get("instance") is not instance:
                        self._global_exception_handlers = None
                        _cache["exc_handlers"] = self._collect_exception_handlers(instance)
                        bound_method = getattr(instance, method_name)
                        _cache["resolver"] = ParameterResolver(bound_method)
                        _cache["method"] = bound_method
                        _cache["instance"] = instance  # set last — acts as init flag

            request.state.pyfly_rest_controller = (
                getattr(controller_cls, "__pyfly_stereotype__", "") == "rest_controller"
            )
            request.state.pyfly_view_controller = not request.state.pyfly_rest_controller
            try:
                converters = getattr(request.app.state, "pyfly_message_converters", None)
            except (KeyError, AttributeError):
                converters = None

            try:
                kwargs = await _cache["resolver"].resolve(request)
                result = await _maybe_await(_cache["method"](**kwargs))
                return await dispatch_response(request, result, status_code, converters=converters)
            except Exception as exc:
                # 1. Check controller-local exception handlers
                for exc_type, handler in _cache["exc_handlers"].items():
                    if isinstance(exc, exc_type):
                        result = await _maybe_await(handler(exc))
                        if isinstance(result, tuple) and len(result) == 2:
                            return JSONResponse(result[1], status_code=result[0])
                        return await dispatch_response(request, result, converters=converters)
                # 2. Check global @controller_advice exception handlers
                for exc_type, handler in self._get_global_advice_handlers(ctx).items():
                    if isinstance(exc, exc_type):
                        result = await _maybe_await(handler(exc))
                        if isinstance(result, tuple) and len(result) == 2:
                            return JSONResponse(result[1], status_code=result[0])
                        return await dispatch_response(request, result, converters=converters)
                raise

        lazy_endpoint.__pyfly_controller_route__ = True  # type: ignore[attr-defined]
        lazy_endpoint.__pyfly_rest_controller__ = (  # type: ignore[attr-defined]
            getattr(controller_cls, "__pyfly_stereotype__", "") == "rest_controller"
        )
        return lazy_endpoint
