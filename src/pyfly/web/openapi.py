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
"""OpenAPI 3.1 schema generator with automatic response schemas, tags, and descriptions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast, get_args, get_origin

from pydantic import BaseModel

if TYPE_CHECKING:
    from pyfly.web.adapters.starlette.controller import RouteMetadata
    from pyfly.web.adapters.starlette.mounted_routes import MountedRoute

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")

# Standard validation error schema (matches FastAPI's 422 response)
_VALIDATION_ERROR_SCHEMA = {
    "title": "ValidationError",
    "type": "object",
    "properties": {
        "detail": {
            "title": "Detail",
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "loc": {
                        "title": "Location",
                        "type": "array",
                        "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                    },
                    "msg": {"title": "Message", "type": "string"},
                    "type": {"title": "Error Type", "type": "string"},
                },
                "required": ["loc", "msg", "type"],
            },
        }
    },
    "required": ["detail"],
}

_HTTP_VALIDATION_ERROR_SCHEMA = {
    "title": "HTTPValidationError",
    "type": "object",
    "properties": {
        "detail": {
            "title": "Detail",
            "type": "array",
            "items": {"$ref": "#/components/schemas/ValidationError"},
        }
    },
}


def _path_slug(path: str) -> str:
    """``/api/telegram/{botId}/updates`` → ``api_telegram_botId_updates``: the part of a
    qualified operationId that names the path, with nothing an identifier cannot carry."""
    return re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")


class OpenAPIGenerator:
    """Generate an OpenAPI 3.1 specification dict.

    Automatically derives:
    - Response schemas from handler return type hints
    - Tags from ``@rest_controller`` class names
    - Summary/description from handler docstrings
    - Validation error responses for endpoints with request bodies
    - Pydantic model schemas with proper ``$ref`` resolution

    Usage::

        gen = OpenAPIGenerator(title="My API", version="1.0.0")
        spec = gen.generate()                       # empty paths
        spec = gen.generate(route_metadata=metadata) # real paths
    """

    def __init__(
        self,
        title: str,
        version: str,
        description: str = "",
    ) -> None:
        self._title = title
        self._version = version
        self._description = description
        self._schemas: dict[str, Any] = {}

    def generate(
        self,
        route_metadata: list[RouteMetadata] | None = None,
        websocket_routes: list[dict[str, str]] | None = None,
        mounted_routes: list[MountedRoute] | None = None,
    ) -> dict[str, Any]:
        """Generate a complete OpenAPI 3.1 spec as a dict.

        ``websocket_routes`` — from ``ControllerRegistrar.collect_websocket_routes()`` — is published
        under the ``x-pyfly-websocket-routes`` extension rather than as operations. WebSocket has no
        OpenAPI representation, but leaving it out entirely made the document quietly incomplete: a
        service could delete a socket route and a CI diff of /openapi.json would report no change.

        ``mounted_routes`` — from ``collect_mounted_routes()`` over ``create_app(extra_routes=...)`` —
        are the plain Starlette routes and mounted sub-applications served beside the controllers.
        They become operations marked ``x-pyfly-mounted: true`` (no typed contract can be read from
        an ASGI endpoint) with their path parameters declared and a unique ``operationId``, and
        never overwrite a controller's operation on the same path and method; a mount that could
        not be walked is listed under ``x-pyfly-mounts``. Same reason as above:
        a service whose webhooks live in sub-apps had a document that described none of them.
        """
        self._schemas = {}

        paths: dict[str, Any] = {}
        tags: list[dict[str, str]] = []
        if route_metadata:
            paths = self._build_paths(route_metadata)
            tags = self._collect_tags(route_metadata)

        # operationIds must be unique across the document (OpenAPI 3.1). A mounted route's name
        # is its endpoint's ``__name__`` unless the route was named, and six sub-applications
        # each carrying a ``health`` endpoint are the normal case, not the exception. The first
        # holder of a name keeps it (the controllers' ids are taken first, so a controller never
        # loses its id to a mounted twin); every later one is qualified by method and path,
        # which is deterministic, so a diff of the document stays stable.
        taken: set[str] = {
            str(operation.get("operationId"))
            for operations in paths.values()
            for operation in operations.values()
            if isinstance(operation, dict) and operation.get("operationId")
        }
        opaque_mounts: list[dict[str, str]] = []
        for mounted in mounted_routes or ():
            if mounted.method is None:
                opaque_mounts.append({"path": mounted.path, "name": mounted.name})
                continue
            operations = paths.setdefault(mounted.path, {})
            method_key = mounted.method.lower()
            if method_key in operations:
                continue
            operation_id = mounted.name
            if operation_id in taken:
                operation_id = f"{mounted.name}_{method_key}_{_path_slug(mounted.path)}"
            taken.add(operation_id)
            operation: dict[str, Any] = {"operationId": operation_id}
            if mounted.summary:
                operation["summary"] = mounted.summary
            if mounted.parameters:
                operation["parameters"] = [parameter.to_openapi() for parameter in mounted.parameters]
            operation["responses"] = {"default": {"description": "Successful response"}}
            operation["x-pyfly-mounted"] = True
            operations[method_key] = operation

        spec: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": self._build_info(),
            "paths": paths,
        }

        if tags:
            spec["tags"] = tags

        if self._schemas:
            spec["components"] = {"schemas": self._schemas}

        if websocket_routes:
            spec["x-pyfly-websocket-routes"] = websocket_routes

        if opaque_mounts:
            spec["x-pyfly-mounts"] = opaque_mounts

        return spec

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def _build_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "title": self._title,
            "version": self._version,
        }
        if self._description:
            info["description"] = self._description
        return info

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_tags(route_metadata: list[RouteMetadata]) -> list[dict[str, str]]:
        """Collect unique tags from route metadata, preserving discovery order."""
        seen: set[str] = set()
        tags: list[dict[str, str]] = []
        for meta in route_metadata:
            if meta.tag and meta.tag not in seen:
                seen.add(meta.tag)
                tags.append({"name": meta.tag})
        return tags

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _build_paths(self, route_metadata: list[RouteMetadata]) -> dict[str, Any]:
        """Build the ``paths`` dict from a list of RouteMetadata."""
        paths: dict[str, Any] = {}

        for meta in route_metadata:
            path = meta.path
            method_key = meta.http_method.lower()

            if path not in paths:
                paths[path] = {}

            operation: dict[str, Any] = {
                "operationId": meta.handler_name,
                "responses": self._build_responses(meta),
            }

            # Tag
            if meta.tag:
                operation["tags"] = [meta.tag]

            # Summary and description from docstrings
            if meta.summary:
                operation["summary"] = meta.summary
            if meta.description:
                operation["description"] = meta.description

            # Deprecated
            if meta.deprecated:
                operation["deprecated"] = True

            # Parameters
            if meta.parameters:
                operation["parameters"] = meta.parameters

            # Request body
            if meta.request_body_model is not None:
                ref = self._register_model(meta.request_body_model)
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": ref},
                        }
                    },
                }

            paths[path][method_key] = operation

        return paths

    # ------------------------------------------------------------------
    # Responses
    # ------------------------------------------------------------------

    def _build_responses(self, meta: RouteMetadata) -> dict[str, Any]:
        """Build the ``responses`` dict for a single operation."""
        status = str(meta.status_code)
        responses: dict[str, Any] = {}

        if meta.status_code == 204:
            responses[status] = {"description": "No Content"}
        elif meta.return_type is not None and self._is_pydantic_model(meta.return_type):
            ref = self._register_model(meta.return_type)
            responses[status] = {
                "description": "Successful response",
                "content": {
                    "application/json": {
                        "schema": {"$ref": ref},
                    }
                },
            }
        elif meta.return_type is not None and self._is_list_of_pydantic(meta.return_type):
            inner = self._get_list_inner_type(meta.return_type)
            ref = self._register_model(inner)
            responses[status] = {
                "description": "Successful response",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "array",
                            "items": {"$ref": ref},
                        }
                    }
                },
            }
        elif meta.media_type != "application/json":
            # An @sse_mapping: the body is a stream of text/event-stream frames, not a JSON document,
            # and saying so is the difference between a described stream and an undescribed one.
            responses[status] = {
                "description": "Event stream",
                "content": {meta.media_type: {"schema": {"type": "string"}}},
            }
        else:
            responses[status] = {"description": "Successful response"}

        # Add 422 Validation Error for endpoints with request bodies
        if meta.request_body_model is not None:
            self._ensure_validation_schemas()
            responses["422"] = {
                "description": "Validation Error",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/HTTPValidationError"},
                    }
                },
            }

        return responses

    # ------------------------------------------------------------------
    # Schema registration
    # ------------------------------------------------------------------

    def _register_model(self, model: type) -> str:
        """Register a Pydantic model in ``components.schemas`` and return a ``$ref`` string.

        Handles Pydantic v2's ``$defs`` by hoisting nested model schemas
        into ``components/schemas`` and rewriting internal ``$ref`` paths.
        """
        name = model.__name__
        if name not in self._schemas:
            schema = model.model_json_schema()  # type: ignore[attr-defined]

            # Hoist $defs into components/schemas
            defs = schema.pop("$defs", None)
            if defs:
                for def_name, def_schema in defs.items():
                    if def_name not in self._schemas:
                        self._schemas[def_name] = self._rewrite_refs(def_schema)

            self._schemas[name] = self._rewrite_refs(schema)

        return f"#/components/schemas/{name}"

    def _ensure_validation_schemas(self) -> None:
        """Register the standard validation error schemas if not already present."""
        if "ValidationError" not in self._schemas:
            self._schemas["ValidationError"] = _VALIDATION_ERROR_SCHEMA
        if "HTTPValidationError" not in self._schemas:
            self._schemas["HTTPValidationError"] = _HTTP_VALIDATION_ERROR_SCHEMA

    @staticmethod
    def _rewrite_refs(schema: Any) -> Any:
        """Rewrite ``$ref: #/$defs/Name`` → ``$ref: #/components/schemas/Name``."""
        if isinstance(schema, dict):
            result = {}
            for key, value in schema.items():
                if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                    result[key] = value.replace("#/$defs/", "#/components/schemas/")
                else:
                    result[key] = OpenAPIGenerator._rewrite_refs(value)
            return result
        if isinstance(schema, list):
            return [OpenAPIGenerator._rewrite_refs(item) for item in schema]
        return schema

    # ------------------------------------------------------------------
    # Type introspection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_pydantic_model(t: type) -> bool:
        """Check if a type is a Pydantic BaseModel subclass."""
        try:
            return isinstance(t, type) and issubclass(t, BaseModel)
        except TypeError:
            return False

    @staticmethod
    def _is_list_of_pydantic(t: Any) -> bool:
        """Check if a type is ``list[SomePydanticModel]``."""
        origin = get_origin(t)
        if origin is list:
            args = get_args(t)
            if args:
                return OpenAPIGenerator._is_pydantic_model(args[0])
        return False

    @staticmethod
    def _get_list_inner_type(t: Any) -> type:
        """Extract the inner type from ``list[T]``."""
        return cast(type, get_args(t)[0])
