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
"""Framework-neutral, documentation-only OpenAPI contracts.

Schema inputs are Python types, annotated aliases, or Pydantic TypeAdapter instances.
They describe the wire format; they never change binding, validation, or authorization.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


class _Unset(Enum):
    VALUE = "unset"


UNSET = _Unset.VALUE
SecurityRequirements = Sequence[Mapping[str, Sequence[str]]]


def _validate_security(security: SecurityRequirements | None) -> None:
    if security is None:
        return
    if isinstance(security, (str, bytes)) or not isinstance(security, Sequence):
        raise TypeError("security must be a sequence of security requirement mappings")
    for requirement in security:
        if not isinstance(requirement, Mapping):
            raise TypeError("security requirements must be mappings")
        for name, scopes in requirement.items():
            if not isinstance(name, str) or not name:
                raise ValueError("security scheme names must be nonempty strings")
            if isinstance(scopes, (str, bytes)) or not isinstance(scopes, Sequence):
                raise TypeError("security scopes must be sequences of strings")
            if any(not isinstance(scope, str) for scope in scopes):
                raise TypeError("security scopes must be strings")


def _validate_description_required(description: str, required: bool) -> None:
    if not isinstance(description, str):
        raise TypeError("description must be a string")
    if not isinstance(required, bool):
        raise TypeError("required must be a bool")


def _validate_content(content: Mapping[str, Any]) -> None:
    if not isinstance(content, Mapping):
        raise TypeError("content must map media types to Python schema types or TypeAdapters")
    for media_type in content:
        if not isinstance(media_type, str) or "/" not in media_type:
            raise ValueError("content keys must be media types, for example application/json")


@dataclass(frozen=True)
class OpenAPIParameter:
    """Document a parameter; ``default`` is omitted unless explicitly supplied.

    Path parameters must be required. ``schema`` is a Python type or TypeAdapter,
    not a raw JSON schema. Explicit operation parameters replace the inferred list.
    """

    name: str
    location: Literal["path", "query", "header", "cookie"]
    schema: Any
    required: bool = True
    description: str = ""
    default: Any = UNSET

    def __post_init__(self) -> None:
        _validate_description_required(self.description, self.required)
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("parameter name must be a nonempty string")
        if self.location not in ("path", "query", "header", "cookie"):
            raise ValueError("parameter location must be path, query, header, or cookie")
        if self.location == "path" and not self.required:
            raise ValueError("path parameters must be required")


@dataclass(frozen=True)
class OpenAPIHeader:
    """Response header schema, generated in serialization mode."""

    schema: Any
    description: str = ""
    required: bool = False

    def __post_init__(self) -> None:
        _validate_description_required(self.description, self.required)


@dataclass(frozen=True)
class OpenAPIRequestBody:
    """Request content maps media types to Python types or TypeAdapters.

    ``required`` concerns the presence of the body, independently of nullable values.
    """

    content: Mapping[str, Any]
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        _validate_description_required(self.description, self.required)
        _validate_content(self.content)
        if not self.content:
            raise ValueError("request body content must not be empty; use request_body=None to omit it")


@dataclass(frozen=True)
class OpenAPIResponse:
    """A complete response for one status; empty content documents a bodyless response."""

    description: str
    content: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, OpenAPIHeader] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.description, str):
            raise TypeError("response description must be a string")
        _validate_content(self.content)
        if not isinstance(self.headers, Mapping):
            raise TypeError("response headers must map names to OpenAPIHeader values")
        for name, header in self.headers.items():
            if not isinstance(name, str) or not name or not isinstance(header, OpenAPIHeader):
                raise TypeError("response headers must map nonempty names to OpenAPIHeader values")


@dataclass(frozen=True)
class OpenAPIOperation:
    """Explicit overrides of inferred operation documentation.

    Unset request_body infers it; None omits it. None parameters/tags infer them;
    empty sequences omit them. None summary/description/deprecated infer them;
    empty strings/False suppress them. None security inherits global security;
    [] explicitly declares no security. These settings never enforce authentication.

    Responses merge with inferred statuses by default, replacing each declared status
    completely (including 422). replace_responses=True replaces the entire response
    map and requires at least one response. Status keys accept 100..599, default,
    and OpenAPI ranges such as 2XX. operation_id overrides mapping name and handler name.
    """

    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: Sequence[str] | None = None
    deprecated: bool | None = None
    parameters: Sequence[OpenAPIParameter] | None = None
    request_body: OpenAPIRequestBody | None | _Unset = UNSET
    responses: Mapping[int | str, OpenAPIResponse] | None = None
    replace_responses: bool = False
    security: SecurityRequirements | None = None

    def __post_init__(self) -> None:
        if self.operation_id is not None and (not isinstance(self.operation_id, str) or not self.operation_id.strip()):
            raise ValueError("operation_id must be a nonempty string")
        for name in ("summary", "description"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
        if self.tags is not None and (
            not isinstance(self.tags, Sequence)
            or isinstance(self.tags, (str, bytes))
            or any(not isinstance(tag, str) for tag in self.tags)
        ):
            raise TypeError("tags must be a sequence of strings")
        if self.deprecated is not None and not isinstance(self.deprecated, bool):
            raise TypeError("deprecated must be a bool or None")
        if self.parameters is not None:
            if not isinstance(self.parameters, Sequence) or any(
                not isinstance(param, OpenAPIParameter) for param in self.parameters
            ):
                raise TypeError("parameters must be a sequence of OpenAPIParameter values")
            identities = [(param.location, param.name) for param in self.parameters]
            if len(identities) != len(set(identities)):
                raise ValueError("duplicate explicit parameters")
        if (
            self.request_body is not UNSET
            and self.request_body is not None
            and not isinstance(self.request_body, OpenAPIRequestBody)
        ):
            raise TypeError("request_body must be OpenAPIRequestBody, None, or unset")
        if self.responses is not None:
            if not isinstance(self.responses, Mapping):
                raise TypeError("responses must map status codes to OpenAPIResponse values")
            seen: set[str] = set()
            for status, response in self.responses.items():
                if not re.fullmatch(r"[1-5](?:[0-9]{2}|XX)|default", str(status)):
                    raise ValueError(f"invalid OpenAPI response status: {status!r}")
                if str(status) in seen:
                    raise ValueError(f"duplicate response status: {status}")
                seen.add(str(status))
                if not isinstance(response, OpenAPIResponse):
                    raise TypeError("responses must contain OpenAPIResponse values")
        if not isinstance(self.replace_responses, bool):
            raise TypeError("replace_responses must be a bool")
        if self.replace_responses and not self.responses:
            raise ValueError("replace_responses requires at least one explicit response")
        _validate_security(self.security)


def openapi_operation(
    *,
    operation_id: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    tags: Sequence[str] | None = None,
    deprecated: bool | None = None,
    parameters: Sequence[OpenAPIParameter] | None = None,
    request_body: OpenAPIRequestBody | None | _Unset = UNSET,
    responses: Mapping[int | str, OpenAPIResponse] | None = None,
    replace_responses: bool = False,
    security: SecurityRequirements | None = None,
) -> Callable[[F], F]:
    """Attach OpenAPIOperation metadata without wrapping or modifying the signature.

    Accepts the same overrides as OpenAPIOperation; see that type for merge/unset
    semantics. May appear above or below an HTTP mapping decorator.
    """
    operation = OpenAPIOperation(
        operation_id=operation_id,
        summary=summary,
        description=description,
        tags=tags,
        deprecated=deprecated,
        parameters=parameters,
        request_body=request_body,
        responses=responses,
        replace_responses=replace_responses,
        security=security,
    )

    def decorate(func: F) -> F:
        target = func.__func__ if isinstance(func, (staticmethod, classmethod)) else func
        target.__pyfly_openapi__ = operation  # type: ignore[attr-defined]
        return func

    return decorate


@dataclass
class RouteMetadata:
    """Offline operation input; no framework, context or handler invocation is needed.

    Existing raw parameter dictionaries remain supported. parameter_types optionally
    supplies Python schema inputs by (location, name), retaining richer annotations.
    operation overrides inference, and mapping_name is the routing decorator's name.
    """

    path: str
    http_method: str
    status_code: int
    handler: Any
    handler_name: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_body_model: Any = None
    return_type: Any = None
    tag: str = ""
    summary: str = ""
    description: str = ""
    deprecated: bool = False
    media_type: str = "application/json"
    mapping_name: str | None = None
    operation: OpenAPIOperation | None = None
    parameter_types: dict[tuple[str, str], Any] = field(default_factory=dict)
