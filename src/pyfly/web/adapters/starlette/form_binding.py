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
"""Bounded form parsing cached in ASGI scope for filters and controllers."""

from __future__ import annotations

import types
from typing import Any, Union, get_args, get_origin
from urllib.parse import parse_qsl

from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.datastructures import FormData
from starlette.requests import Request

from pyfly.kernel.exceptions import InvalidRequestException, PayloadTooLargeException, UnsupportedMediaTypeException
from pyfly.web.forms import FormProperties, FormValidationException


def properties_for(request: Request) -> FormProperties:
    config = getattr(request.app.state, "pyfly_config", None) if "app" in request.scope else None
    return config.bind(FormProperties) if config is not None else FormProperties()


async def parse_form(request: Request) -> FormData:
    cached = request.scope.get("pyfly.form")
    if isinstance(cached, FormData):
        return cached
    props = properties_for(request)
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media not in ("application/x-www-form-urlencoded", "multipart/form-data"):
        raise UnsupportedMediaTypeException("Expected an HTML form")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > props.max_body_size:
            raise PayloadTooLargeException("Form body exceeds configured limit")
    body = bytes(content)
    request.scope["pyfly.request_body"] = body
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    if media == "application/x-www-form-urlencoded":
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=props.max_fields)
        except (UnicodeError, ValueError) as exc:
            raise InvalidRequestException("Invalid form or too many fields") from exc
        if any(len(value.encode()) > props.max_part_size for _, value in pairs):
            raise PayloadTooLargeException("Form field exceeds configured limit")
        form = FormData([(name, value) for name, value in pairs])
    else:
        parsed = Request(request.scope, receive=receive)
        form = await parsed.form(
            max_fields=props.max_fields, max_files=props.max_files, max_part_size=props.max_part_size
        )
        if any(
            getattr(value, "size", 0) > props.max_part_size
            for _, value in form.multi_items()
            if not isinstance(value, str)
        ):
            await form.close()
            raise PayloadTooLargeException("Uploaded file exceeds configured limit")
    request.scope["pyfly.form"] = form
    return form


def _is_list(annotation: Any) -> bool:
    if get_origin(annotation) in (Union, types.UnionType):
        return any(_is_list(arg) for arg in get_args(annotation))
    return get_origin(annotation) is list


async def bind_form(request: Request, name: str, annotation: Any, default: Any, has_default: bool) -> Any:
    form = await parse_form(request)
    is_model = isinstance(annotation, type) and issubclass(annotation, BaseModel)
    fields = annotation.model_fields if is_model else {}
    values: dict[str, Any] = {}
    safe: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for field_name, hint in fields.items() if is_model else [(name, None)]:
        alias = (hint.alias or field_name) if hint else field_name
        field_type = hint.annotation if hint else annotation
        items = form.getlist(alias)
        if not items:
            continue
        if any(not isinstance(value, str) for value in items):
            errors.append({"loc": [alias], "msg": "Expected text", "type": "form_type"})
            continue
        if len(items) > 1 and not _is_list(field_type):
            errors.append({"loc": [alias], "msg": "Repeated scalar field", "type": "form_duplicate"})
        value = items if _is_list(field_type) else items[0]
        values[alias] = value
        if not any(
            part in f"{field_name} {alias}".lower() for part in ("password", "secret", "token", "csrf")
        ) and "Secret" not in str(field_type):
            safe[alias] = value
    if errors:
        raise FormValidationException(errors, safe)
    raw = values if is_model else values.get(name, default if has_default else None)
    try:
        return TypeAdapter(annotation).validate_python(raw)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": "Invalid value", "type": e["type"]} for e in exc.errors()]
        raise FormValidationException(errors, safe) from exc
