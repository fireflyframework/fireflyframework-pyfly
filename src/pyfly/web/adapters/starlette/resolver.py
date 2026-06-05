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
"""ParameterResolver — inspects handler signatures and auto-binds from Request."""

from __future__ import annotations

import inspect
import types
import typing
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel
from starlette.requests import Request

from pyfly.kernel.exceptions import InvalidRequestException
from pyfly.web.params import Body, Cookie, File, Header, PathVar, QueryParam, UploadedFile, Valid

_BINDING_TYPES = {PathVar, QueryParam, Body, Header, Cookie, File}
_MISSING = object()


def _permits_none(tp: Any) -> bool:
    """True if the type annotation explicitly allows None (Optional[T] / T | None / NoneType)."""
    if tp is type(None):
        return True
    if get_origin(tp) is Union or isinstance(tp, types.UnionType):
        return any(a is type(None) for a in get_args(tp))
    return False


@dataclass
class ResolvedParam:
    """Metadata for a single resolved parameter."""

    name: str
    binding_type: type
    inner_type: type
    default: Any = _MISSING
    validate: bool = False
    required: bool = True


class ParameterResolver:
    """Inspects a handler method's signature and resolves parameters from a Request.

    At startup, inspects type hints to detect PathVar, QueryParam, Body, Header, Cookie.
    At runtime, resolves each parameter from the Starlette Request.
    """

    def __init__(self, handler: Any) -> None:
        self.params = self._inspect(handler)

    def _inspect(self, handler: Any) -> list[ResolvedParam]:
        hints = typing.get_type_hints(handler, include_extras=True)
        sig = inspect.signature(handler)
        params: list[ResolvedParam] = []

        for name, param in sig.parameters.items():
            if name == "self":
                continue

            hint = hints.get(name)
            if hint is None:
                continue

            # Support injecting the raw Starlette Request
            if hint is Request:
                params.append(ResolvedParam(name=name, binding_type=Request, inner_type=Request))
                continue

            origin = get_origin(hint)
            validate = False

            # Unwrap Valid[T]: peel the Valid layer to find the inner binding type
            if origin is Valid:
                validate = True
                inner_args = get_args(hint)
                if not inner_args:
                    continue
                inner_hint = inner_args[0]
                inner_origin = get_origin(inner_hint)

                if inner_origin in _BINDING_TYPES:
                    # Valid[Body[T]], Valid[QueryParam[T]], etc.
                    origin = inner_origin
                    hint = inner_hint
                else:
                    # Valid[T] standalone → implies Body[T]
                    origin = Body
                    hint = Body[inner_hint]  # type: ignore[valid-type]

            if origin not in _BINDING_TYPES:
                continue

            args = get_args(hint)
            inner_type = args[0] if args else str
            default = param.default if param.default is not inspect.Parameter.empty else _MISSING
            # A param is required when it has no Python default and its type does not admit None.
            # Body is validated/handled separately, so only scalar bindings enforce presence here.
            required = default is _MISSING and not _permits_none(inner_type)

            params.append(
                ResolvedParam(
                    name=name,
                    binding_type=origin,
                    inner_type=inner_type,
                    default=default,
                    validate=validate,
                    required=required,
                )
            )

        return params

    async def resolve(self, request: Request) -> dict[str, Any]:
        """Resolve all parameters from the request."""
        kwargs: dict[str, Any] = {}
        for param in self.params:
            value = await self._resolve_one(request, param)
            if param.validate:
                value = self._run_validation(value, param)
            kwargs[param.name] = value
        return kwargs

    async def _resolve_one(self, request: Request, param: ResolvedParam) -> Any:
        if param.binding_type is Request:
            return request
        if param.binding_type is PathVar:
            return self._resolve_path_var(request, param)
        if param.binding_type is QueryParam:
            return self._resolve_query_param(request, param)
        if param.binding_type is Body:
            return await self._resolve_body(request, param)
        if param.binding_type is Header:
            return self._resolve_header(request, param)
        if param.binding_type is Cookie:
            return self._resolve_cookie(request, param)
        if param.binding_type is File:
            return await self._resolve_file(request, param)
        return None  # pragma: no cover

    def _resolve_path_var(self, request: Request, param: ResolvedParam) -> Any:
        raw = request.path_params.get(param.name)
        if raw is None:
            if param.default is not _MISSING:
                return param.default
            msg = f"Missing path variable: {param.name}"
            raise ValueError(msg)
        return self._coerce(raw, param.inner_type)

    def _resolve_query_param(self, request: Request, param: ResolvedParam) -> Any:
        raw = request.query_params.get(param.name)
        if raw is None:
            if param.default is not _MISSING:
                return param.default
            if param.required:
                raise InvalidRequestException(
                    f"Missing required query parameter: {param.name}",
                    code="MISSING_PARAMETER",
                    context={"parameter": param.name, "location": "query"},
                )
            return None
        return self._coerce(raw, param.inner_type)

    async def _resolve_body(self, request: Request, param: ResolvedParam) -> Any:
        body_bytes = await request.body()
        if issubclass(param.inner_type, BaseModel):
            if param.validate:
                # Valid[Body[T]] or Valid[T]: catch Pydantic errors for structured 422
                from pydantic import ValidationError as PydanticValidationError

                from pyfly.kernel.exceptions import ValidationException

                try:
                    return param.inner_type.model_validate_json(body_bytes)
                except PydanticValidationError as exc:
                    errors = exc.errors()
                    detail = "; ".join(f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in errors)
                    raise ValidationException(
                        f"Validation failed: {detail}",
                        code="VALIDATION_ERROR",
                        context={"errors": errors},
                    ) from exc
            return param.inner_type.model_validate_json(body_bytes)
        return param.inner_type(body_bytes.decode())  # type: ignore[call-arg]

    def _resolve_header(self, request: Request, param: ResolvedParam) -> Any:
        header_name = param.name.replace("_", "-")
        raw = request.headers.get(header_name)
        if raw is None:
            if param.default is not _MISSING:
                return param.default
            if param.required:
                raise InvalidRequestException(
                    f"Missing required header: {header_name}",
                    code="MISSING_PARAMETER",
                    context={"parameter": header_name, "location": "header"},
                )
            return None
        return self._coerce(raw, param.inner_type)

    def _resolve_cookie(self, request: Request, param: ResolvedParam) -> Any:
        raw = request.cookies.get(param.name)
        if raw is None:
            if param.default is not _MISSING:
                return param.default
            if param.required:
                raise InvalidRequestException(
                    f"Missing required cookie: {param.name}",
                    code="MISSING_PARAMETER",
                    context={"parameter": param.name, "location": "cookie"},
                )
            return None
        return self._coerce(raw, param.inner_type)

    def _run_validation(self, value: Any, param: ResolvedParam) -> Any:
        """Run Pydantic validation on a resolved value.

        For BaseModel instances (already validated by model_validate_json), this
        re-validates to produce structured 422 errors via ValidationException.
        For dicts, validates against the inner_type model.
        """
        if value is None:
            return value

        if isinstance(value, BaseModel):
            # Already a model instance (from Body resolution) — it's valid
            return value

        if isinstance(value, dict) and isinstance(param.inner_type, type) and issubclass(param.inner_type, BaseModel):
            from pyfly.validation.helpers import validate_model

            return validate_model(param.inner_type, value)

        return value

    def _coerce(self, value: str, target_type: type) -> Any:
        """Coerce a string value to the target type.

        Handles ``Optional[T]`` / ``T | None`` union types by unwrapping to the
        first non-NoneType argument before coercion.
        """
        # Unwrap Optional[T] (typing.Union[T, None]) or T | None (types.UnionType)
        actual_type = target_type
        origin = get_origin(target_type)
        if origin is Union or isinstance(target_type, types.UnionType):
            non_none = [a for a in get_args(target_type) if a is not type(None)]
            actual_type = non_none[0] if non_none else str

        if actual_type is str:
            return value
        try:
            return actual_type(value)
        except (ValueError, TypeError) as exc:
            type_name = getattr(actual_type, "__name__", repr(actual_type))
            raise InvalidRequestException(
                f"Cannot convert '{value}' to {type_name}",
                code="TYPE_CONVERSION_ERROR",
            ) from exc

    async def _resolve_file(self, request: Request, param: ResolvedParam) -> Any:
        """Resolve a File[UploadedFile] or File[list[UploadedFile]] parameter."""
        form = await request.form()

        # Check if inner type is list[UploadedFile] (multi-file)
        if get_origin(param.inner_type) is list:
            files = form.getlist(param.name)
            return [self._wrap_upload(f) for f in files if hasattr(f, "filename")]

        # Single file
        upload = form.get(param.name)
        if upload is None or not hasattr(upload, "filename"):
            if param.default is not _MISSING:
                return param.default
            return None
        return self._wrap_upload(upload)

    @staticmethod
    def _wrap_upload(upload: Any) -> UploadedFile:
        """Wrap a Starlette UploadFile into PyFly's UploadedFile."""
        return UploadedFile(
            filename=upload.filename or "",
            content_type=upload.content_type or "application/octet-stream",
            size=upload.size or 0,
            _file=upload.file,
        )
