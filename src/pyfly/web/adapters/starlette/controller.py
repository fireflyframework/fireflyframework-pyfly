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
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.adapters.starlette.response import handle_return_value
from pyfly.web.params import Body, Cookie, Header, PathVar, QueryParam, inspect_binding

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


@dataclass
class RouteMetadata:
    """Metadata extracted from a single controller handler method."""

    path: str
    http_method: str
    status_code: int
    handler: Any
    handler_name: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_body_model: type | None = None
    return_type: type | None = None
    tag: str = ""
    summary: str = ""
    description: str = ""
    deprecated: bool = False


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
                method_obj = getattr(cls, attr_name, None)
                if method_obj is None:
                    continue

                mapping = getattr(method_obj, "__pyfly_mapping__", None)
                if mapping is None:
                    continue

                full_path = base_path + mapping["path"]
                http_method = mapping["method"]
                status_code = mapping.get("status_code", 200)

                handler = self._make_lazy_handler(ctx, cls, attr_name, status_code)
                routes.append(Route(full_path, handler, methods=[http_method]))

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
                method_obj = getattr(cls, attr_name, None)
                if method_obj is None:
                    continue

                mapping = getattr(method_obj, "__pyfly_mapping__", None)
                if mapping is None:
                    continue

                full_path = base_path + mapping["path"]
                http_method = mapping["method"]
                status_code = mapping.get("status_code", 200)

                # Extract parameter metadata and request body model from type hints
                params, body_model = self._extract_param_metadata(method_obj)

                # Extract return type
                hints = typing.get_type_hints(method_obj, include_extras=True)
                return_type = hints.get("return")

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
                    )
                )

        return metadata

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

    def _extract_param_metadata(self, handler: Any) -> tuple[list[dict[str, Any]], type | None]:
        """Extract OpenAPI parameter dicts and request body model from handler type hints."""
        from pydantic import BaseModel

        hints = typing.get_type_hints(handler, include_extras=True)
        sig = inspect.signature(handler)
        params: list[dict[str, Any]] = []
        body_model: type | None = None

        for name, param in sig.parameters.items():
            if name == "self":
                continue

            hint = hints.get(name)
            if hint is None:
                continue

            binding, inner_type, _validate = inspect_binding(hint)
            if binding is None:
                continue

            default = param.default if param.default is not inspect.Parameter.empty else _MISSING

            if binding is PathVar:
                params.append(
                    {
                        "name": name,
                        "in": "path",
                        "required": True,
                        "schema": {"type": _py_type_to_openapi(inner_type)},
                    }
                )
            elif binding is QueryParam:
                p: dict[str, Any] = {
                    "name": name,
                    "in": "query",
                    "required": default is _MISSING,
                    "schema": {"type": _py_type_to_openapi(inner_type)},
                }
                if default is not _MISSING:
                    p["schema"]["default"] = default
                params.append(p)
            elif binding is Header:
                params.append(
                    {
                        "name": name.replace("_", "-"),
                        "in": "header",
                        "required": default is _MISSING,
                        "schema": {"type": _py_type_to_openapi(inner_type)},
                    }
                )
            elif binding is Cookie:
                params.append(
                    {
                        "name": name,
                        "in": "cookie",
                        "required": default is _MISSING,
                        "schema": {"type": _py_type_to_openapi(inner_type)},
                    }
                )
            elif binding is Body and isinstance(inner_type, type) and issubclass(inner_type, BaseModel):
                body_model = inner_type

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
            if "instance" not in _cache:
                async with _init_lock:
                    if "instance" not in _cache:
                        instance = ctx.get_bean(controller_cls)
                        _cache["exc_handlers"] = self._collect_exception_handlers(instance)
                        bound_method = getattr(instance, method_name)
                        _cache["resolver"] = ParameterResolver(bound_method)
                        _cache["method"] = bound_method
                        _cache["instance"] = instance  # set last — acts as init flag

            accept = request.headers.get("accept")
            try:
                converters = getattr(request.app.state, "pyfly_message_converters", None)
            except (KeyError, AttributeError):
                converters = None

            try:
                kwargs = await _cache["resolver"].resolve(request)
                result = await _maybe_await(_cache["method"](**kwargs))
                return handle_return_value(result, status_code, accept=accept, converters=converters)
            except Exception as exc:
                # 1. Check controller-local exception handlers
                for exc_type, handler in _cache["exc_handlers"].items():
                    if isinstance(exc, exc_type):
                        result = await _maybe_await(handler(exc))
                        if isinstance(result, tuple) and len(result) == 2:
                            return JSONResponse(result[1], status_code=result[0])
                        return handle_return_value(result, accept=accept, converters=converters)
                # 2. Check global @controller_advice exception handlers
                for exc_type, handler in self._get_global_advice_handlers(ctx).items():
                    if isinstance(exc, exc_type):
                        result = await _maybe_await(handler(exc))
                        if isinstance(result, tuple) and len(result) == 2:
                            return JSONResponse(result[1], status_code=result[0])
                        return handle_return_value(result, accept=accept, converters=converters)
                raise

        return lazy_endpoint
