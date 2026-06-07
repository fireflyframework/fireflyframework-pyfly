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
"""Controller discovery and route registration for FastAPI."""

from __future__ import annotations

import inspect
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pyfly.web.adapters.starlette.controller import ControllerRegistrar, RouteMetadata
from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.adapters.starlette.response import handle_return_value


async def _maybe_await(result: Any) -> Any:
    """Await the result if it's a coroutine, otherwise return as-is."""
    if inspect.isawaitable(result):
        return await result
    return result


class FastAPIControllerRegistrar:
    """Discovers ``@rest_controller`` and ``@controller`` beans and registers FastAPI routes.

    For each controller:
    1. Reads @request_mapping base path from the class
    2. Finds @*_mapping handler methods
    3. Uses ``app.add_api_route()`` to register each endpoint
    4. Creates lazy handlers that resolve the controller bean on first request

    Reuses the Starlette :class:`ParameterResolver` and :func:`handle_return_value`
    since FastAPI extends Starlette.

    Also collects ``@controller_advice`` beans for global exception handling.
    """

    _CONTROLLER_STEREOTYPES = ("rest_controller", "controller")

    def __init__(self) -> None:
        self._global_exception_handlers: dict[type[Exception], Any] | None = None

    def register_controllers(self, app: Any, ctx: Any) -> None:
        """Discover all ``@rest_controller`` and ``@controller`` beans and register routes.

        Bean resolution is deferred until the first HTTP request hits each
        controller, avoiding eager resolution of the full dependency tree
        during ``create_app()`` (before auto-configurations have run).

        Routes are registered with ``include_in_schema=False`` because FastAPI's
        own introspection cannot read the PyFly parameter-binding metadata and
        would emit an empty/useless schema. The real OpenAPI document is built by
        the application factory using :class:`pyfly.web.openapi.OpenAPIGenerator`
        together with :meth:`collect_route_metadata`.
        """
        # Idempotent: skip (path, method) pairs already registered so a
        # post-start rescan only mounts @bean-produced controllers that were
        # not yet discoverable at create_app time (audit #163).
        existing: set[tuple[str, str]] = set()
        for route in getattr(app, "routes", []):
            for m in getattr(route, "methods", None) or ():
                existing.add((getattr(route, "path", ""), m))

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

                if (full_path, http_method) in existing:
                    continue
                existing.add((full_path, http_method))

                handler = self._make_lazy_handler(ctx, cls, attr_name, status_code)
                app.add_api_route(
                    full_path,
                    handler,
                    methods=[http_method],
                    status_code=status_code,
                    include_in_schema=False,
                )

    def collect_route_metadata(self, ctx: Any) -> list[RouteMetadata]:
        """Collect route metadata from ``@rest_controller`` and ``@controller`` classes.

        Delegates to the Starlette :class:`ControllerRegistrar`. The metadata is
        derived purely from class-level type hints, mappings, and docstrings (no
        bean resolution), so it is adapter-agnostic and reused as-is to feed the
        :class:`pyfly.web.openapi.OpenAPIGenerator`.
        """
        return ControllerRegistrar().collect_route_metadata(ctx)

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
        """Create a FastAPI endpoint that lazily resolves the controller bean on first request."""
        _cache: dict[str, Any] = {}

        async def lazy_endpoint(request: Request) -> Response:
            if "instance" not in _cache:
                _cache["instance"] = ctx.get_bean(controller_cls)
                _cache["exc_handlers"] = self._collect_exception_handlers(_cache["instance"])
                bound_method = getattr(_cache["instance"], method_name)
                _cache["resolver"] = ParameterResolver(bound_method)
                _cache["method"] = bound_method
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
