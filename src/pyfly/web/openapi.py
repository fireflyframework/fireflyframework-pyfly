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
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode

from pyfly.web.openapi_metadata import (
    UNSET,
    OpenAPIOperation,
    OpenAPIRequestBody,
    OpenAPIResponse,
    RouteMetadata,
    SecurityRequirements,
    _validate_security,
)
from pyfly.web.openapi_schema import SchemaRegistry

if TYPE_CHECKING:
    from pyfly.web.adapters.starlette.mounted_routes import MountedRoute

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
    return re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")


class OpenAPIGenerator:
    """Generate OpenAPI 3.1 without acquiring beans or invoking handlers.

    All typed inputs share Pydantic definitions. Requests/parameters use validation
    mode; responses/headers use serialization mode, both by alias. schema_generator
    accepts a GenerateJsonSchema subclass. Security settings describe documentation
    only; use runtime security filters to enforce authentication and authorization.
    """

    def __init__(
        self,
        title: str,
        version: str,
        description: str = "",
        *,
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        security_schemes: Mapping[str, dict[str, Any]] | None = None,
        security: SecurityRequirements | None = None,
    ) -> None:
        self._title = title
        self._version = version
        self._description = description
        self._schema_generator = schema_generator
        self._security_schemes = deepcopy(dict(security_schemes or {}))
        _validate_security(security)
        self._security = deepcopy(security)

    def generate(
        self,
        route_metadata: list[RouteMetadata] | None = None,
        websocket_routes: list[dict[str, str]] | None = None,
        mounted_routes: list[MountedRoute] | None = None,
    ) -> dict[str, Any]:
        """Build a fresh document; controller operations take precedence over mounts.

        Duplicate controller path/method declarations and explicit operation IDs fail.
        Implicit ID collisions are resolved in sorted path/method order, controllers
        first, with suffixes checked against every preferred name in the document.
        Opaque mounts and WebSockets retain their x-pyfly-* inventory extensions.
        """
        registry = SchemaRegistry(self._schema_generator)
        paths: dict[str, Any] = {}
        entries: list[tuple[str, str, str, bool, bool]] = []
        validation_keys: list[tuple[str, str]] = []
        for meta in sorted(route_metadata or (), key=lambda item: (item.path, item.http_method.lower())):
            method = meta.http_method.lower()
            if method in paths.get(meta.path, {}):
                raise ValueError(f"Duplicate controller operation: {method.upper()} {meta.path}")
            override = meta.operation or OpenAPIOperation()
            operation = self._operation(meta, override, registry)
            paths.setdefault(meta.path, {})[method] = operation
            preferred = override.operation_id or meta.mapping_name or meta.handler_name
            entries.append((meta.path, method, preferred, override.operation_id is not None, False))
            if operation["responses"].get("422") is _INFERRED_VALIDATION:
                validation_keys.append((meta.path, method))
                operation["responses"].pop("422")

        opaque_mounts: list[dict[str, str]] = []
        for mounted in sorted(mounted_routes or (), key=lambda item: (item.path, item.method or "", item.name)):
            if mounted.method is None:
                opaque_mounts.append({"path": mounted.path, "name": mounted.name})
                continue
            method = mounted.method.lower()
            if method in paths.get(mounted.path, {}):
                continue
            operation = {"responses": {"default": {"description": "Successful response"}}, "x-pyfly-mounted": True}
            if mounted.summary:
                operation["summary"] = mounted.summary
            if mounted.parameters:
                operation["parameters"] = [parameter.to_openapi() for parameter in mounted.parameters]
            paths.setdefault(mounted.path, {})[method] = operation
            entries.append((mounted.path, method, mounted.name, False, True))
        self._assign_ids(paths, entries)
        info = {"title": self._title, "version": self._version}
        if self._description:
            info["description"] = self._description
        spec: dict[str, Any] = {"openapi": "3.1.0", "info": info, "paths": paths}
        tags = sorted({tag for ops in paths.values() for op in ops.values() for tag in op.get("tags", [])})
        if tags:
            spec["tags"] = [{"name": tag} for tag in tags]
        if self._security is not None:
            spec["security"] = [dict(requirement) for requirement in self._security]
        if websocket_routes:
            spec["x-pyfly-websocket-routes"] = deepcopy(websocket_routes)
        if opaque_mounts:
            spec["x-pyfly-mounts"] = opaque_mounts

        # Inject built-in errors after user names are known: user HTTPValidationError
        # models must never overwrite the built-in error (or vice versa).
        spec, schemas = registry.resolve(spec)
        if validation_keys:
            names: list[str] = []
            for base in ("ValidationError", "HTTPValidationError"):
                name = base
                suffix = 2
                while name in schemas or name in names:
                    name = f"{base}_{suffix}"
                    suffix += 1
                names.append(name)
            schemas[names[0]] = deepcopy(_VALIDATION_ERROR_SCHEMA)
            schemas[names[1]] = deepcopy(_HTTP_VALIDATION_ERROR_SCHEMA)
            schemas[names[1]]["properties"]["detail"]["items"]["$ref"] = f"#/components/schemas/{names[0]}"
            for path, method in validation_keys:
                spec["paths"][path][method]["responses"]["422"] = {
                    "description": "Validation Error",
                    "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{names[1]}"}}},
                }
        components: dict[str, Any] = {}
        if schemas:
            components["schemas"] = schemas
        if self._security_schemes:
            components["securitySchemes"] = deepcopy(self._security_schemes)
        if components:
            spec["components"] = components
        return spec

    @staticmethod
    def _assign_ids(paths: dict[str, Any], entries: list[tuple[str, str, str, bool, bool]]) -> None:
        explicit: set[str] = set()
        for _path, _method, preferred, is_explicit, _mounted in entries:
            if is_explicit:
                if preferred in explicit:
                    raise ValueError(f"Duplicate explicit operation ID: {preferred}")
                explicit.add(preferred)
        reserved = {entry[2] for entry in entries}
        taken = set(explicit)
        for path, method, preferred, is_explicit, _mounted in sorted(entries, key=lambda e: (e[4], e[0], e[1])):
            if is_explicit:
                operation_id = preferred
            elif preferred not in taken:
                operation_id = preferred
                taken.add(operation_id)
            else:
                base = f"{preferred}_{method}_{_path_slug(path)}"
                operation_id = base
                suffix = 2
                while operation_id in taken or operation_id in reserved:
                    operation_id = f"{base}_{suffix}"
                    suffix += 1
                taken.add(operation_id)
            paths[path][method]["operationId"] = operation_id

    def _operation(self, meta: RouteMetadata, override: OpenAPIOperation, registry: SchemaRegistry) -> dict[str, Any]:
        context = f"{meta.http_method.upper()} {meta.path}"
        operation: dict[str, Any] = {}
        for key in ("summary", "description", "deprecated"):
            value = getattr(override, key)
            if value is None:
                value = getattr(meta, key)
            if value:
                operation[key] = value
        tags = override.tags if override.tags is not None else ([meta.tag] if meta.tag else [])
        if tags:
            operation["tags"] = list(tags)
        if override.security is not None:
            operation["security"] = deepcopy([dict(requirement) for requirement in override.security])

        parameters: list[dict[str, Any]] = []
        if override.parameters is not None:
            for parameter in override.parameters:
                item: dict[str, Any] = {
                    "name": parameter.name,
                    "in": parameter.location,
                    "required": parameter.required,
                    "schema": registry.schema(
                        parameter.schema, "validation", context=context, default=parameter.default
                    ),
                }
                if parameter.description:
                    item["description"] = parameter.description
                parameters.append(item)
        else:
            parameters = deepcopy(meta.parameters)
            for item in parameters:
                identity = (item["in"], item["name"])
                if identity in meta.parameter_types:
                    item["schema"] = registry.schema(
                        meta.parameter_types[identity],
                        "validation",
                        context=context,
                        default=item.get("schema", {}).get("default", UNSET),
                    )
        if parameters:
            operation["parameters"] = parameters

        body = override.request_body
        if body is UNSET and meta.request_body_model is not None:
            body = OpenAPIRequestBody(content={"application/json": meta.request_body_model})
        if isinstance(body, OpenAPIRequestBody):
            operation["requestBody"] = {
                "required": body.required,
                "content": self._content(body.content, "validation", registry, context),
            }
            if body.description:
                operation["requestBody"]["description"] = body.description

        responses: dict[str, Any] = {}
        if not override.replace_responses:
            status = str(meta.status_code)
            # A status override also suppresses generation of its inferred schema.
            if not any(str(key) == status for key in override.responses or {}):
                response: dict[str, Any] = {"description": "Successful response"}
                if meta.status_code == 204:
                    response = {"description": "No Content"}
                elif meta.media_type != "application/json":
                    response = {
                        "description": "Event stream",
                        "content": {meta.media_type: {"schema": {"type": "string"}}},
                    }
                elif meta.return_type is not None and meta.return_type is not type(None):
                    schema = registry.schema(meta.return_type, "serialization", context=context, explicit=False)
                    if schema is not None:
                        response["content"] = {"application/json": {"schema": schema}}
                responses[status] = response
            if "requestBody" in operation:
                responses["422"] = _INFERRED_VALIDATION
        for status_key, response_override in (override.responses or {}).items():
            responses[str(status_key)] = self._response(response_override, registry, context)
        operation["responses"] = responses
        return operation

    @staticmethod
    def _content(
        content: Mapping[str, Any], mode: JsonSchemaMode, registry: SchemaRegistry, context: str
    ) -> dict[str, Any]:
        return {media: {"schema": registry.schema(schema, mode, context=context)} for media, schema in content.items()}

    def _response(self, response: OpenAPIResponse, registry: SchemaRegistry, context: str) -> dict[str, Any]:
        result: dict[str, Any] = {"description": response.description}
        if response.content:
            result["content"] = self._content(response.content, "serialization", registry, context)
        if response.headers:
            headers: dict[str, Any] = {}
            for name, header in response.headers.items():
                item: dict[str, Any] = {"schema": registry.schema(header.schema, "serialization", context=context)}
                if header.description:
                    item["description"] = header.description
                if header.required:
                    item["required"] = True
                headers[name] = item
            result["headers"] = headers
        return result


_INFERRED_VALIDATION = object()
