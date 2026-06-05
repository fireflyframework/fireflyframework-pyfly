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
"""Converter utilities — exception translation and data format conversion.

Exception converters: chain of responsibility for translating external library
exceptions (Pydantic, JSON, SQLAlchemy, etc.) into PyFly exceptions.

XML converters: dict/BaseModel to XML string and XML string to dict using
Python's stdlib ``xml.etree.ElementTree`` (no extra dependencies).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from pyfly.kernel.exceptions import (
    InvalidRequestException,
    OperationTimeoutException,
    PyFlyException,
    ValidationException,
)

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


@runtime_checkable
class ExceptionConverter(Protocol):
    """Converts external exceptions to PyFly exceptions."""

    def can_handle(self, exc: Exception) -> bool: ...

    def convert(self, exc: Exception) -> PyFlyException: ...


class ExceptionConverterService:
    """Chain of responsibility for exception conversion.

    Iterates through registered converters and returns the first match.
    """

    def __init__(self, converters: list[ExceptionConverter]) -> None:
        self._converters = converters

    def convert(self, exc: Exception) -> PyFlyException | None:
        """Convert an exception, returning None if no converter matches."""
        for converter in self._converters:
            if converter.can_handle(exc):
                return converter.convert(exc)
        return None


class PydanticExceptionConverter:
    """Converts Pydantic ValidationError to PyFly ValidationException."""

    def can_handle(self, exc: Exception) -> bool:
        return isinstance(exc, ValidationError)

    def convert(self, exc: Exception) -> PyFlyException:
        assert isinstance(exc, ValidationError)
        errors = exc.errors()
        detail = "; ".join(f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in errors)
        return ValidationException(
            f"Validation failed: {detail}",
            code="VALIDATION_ERROR",
            context={"errors": errors},
        )


class JSONExceptionConverter:
    """Converts json.JSONDecodeError to PyFly InvalidRequestException."""

    def can_handle(self, exc: Exception) -> bool:
        return isinstance(exc, json.JSONDecodeError)

    def convert(self, exc: Exception) -> PyFlyException:
        assert isinstance(exc, json.JSONDecodeError)
        return InvalidRequestException(
            f"Invalid JSON: {exc.msg}",
            code="INVALID_JSON",
            context={"position": exc.pos},
        )


class TimeoutExceptionConverter:
    """Converts ``TimeoutError`` (and ``asyncio.TimeoutError``) to a 504-mapped exception."""

    def can_handle(self, exc: Exception) -> bool:
        return isinstance(exc, TimeoutError)

    def convert(self, exc: Exception) -> PyFlyException:
        return OperationTimeoutException(
            "Operation timed out",
            code="OPERATION_TIMEOUT",
        )


def default_exception_converters() -> list[ExceptionConverter]:
    """The built-in converter chain consulted for non-PyFly exceptions.

    Mirrors Spring's registered ``*ExceptionConverter`` chain (validation, JSON,
    timeout). Users can contribute additional converters as beans implementing
    :class:`ExceptionConverter`; they are appended via
    :func:`build_exception_converter_service`.
    """
    return [
        PydanticExceptionConverter(),
        JSONExceptionConverter(),
        TimeoutExceptionConverter(),
    ]


def build_exception_converter_service(
    context: ApplicationContext | None = None,
) -> ExceptionConverterService:
    """Build the global :class:`ExceptionConverterService`.

    Combines the built-in converter chain with any user-provided
    :class:`ExceptionConverter` beans discovered in the application context, so
    arbitrary library exceptions are translated to the correct HTTP status
    before the global handler responds (audit #202).
    """
    converters = default_exception_converters()
    if context is not None:
        for reg in context.container._registrations.values():
            inst = reg.instance
            if inst is not None and isinstance(inst, ExceptionConverter) and not isinstance(inst, type):
                converters.append(inst)
    return ExceptionConverterService(converters)


# ---------------------------------------------------------------------------
# XML data conversion utilities
# ---------------------------------------------------------------------------


def _build_element(parent: ET.Element, key: str, value: Any) -> None:
    """Recursively attach *value* to *parent* as a child element named *key*."""
    if isinstance(value, BaseModel):
        _build_element(parent, key, value.model_dump(mode="json"))
    elif isinstance(value, dict):
        child = ET.SubElement(parent, key)
        for k, v in value.items():
            _build_element(child, k, v)
    elif isinstance(value, list):
        for item in value:
            _build_element(parent, key, item)
    elif value is None:
        child = ET.SubElement(parent, key)
    else:
        child = ET.SubElement(parent, key)
        child.text = str(value)


def dict_to_xml(data: Any, root_tag: str = "response") -> str:
    """Convert a dict, list, BaseModel, or primitive to an XML string.

    - ``BaseModel`` instances are converted via ``model_dump(mode="json")``.
    - ``list`` values produce repeated sibling elements named ``<item>``.
    - ``None`` produces an empty element.
    - Primitives are rendered as text content of the root element.
    """
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")

    root = ET.Element(root_tag)

    if isinstance(data, dict):
        for key, value in data.items():
            _build_element(root, key, value)
    elif isinstance(data, list):
        for item in data:
            _build_element(root, "item", item)
    else:
        root.text = str(data)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _element_to_dict(element: ET.Element) -> dict[str, Any] | str | None:
    """Recursively convert an XML element to a dict, string, or None."""
    children = list(element)
    if not children:
        return element.text

    result: dict[str, Any] = {}
    for child in children:
        child_value = _element_to_dict(child)
        tag = child.tag
        if tag in result:
            existing = result[tag]
            if isinstance(existing, list):
                existing.append(child_value)
            else:
                result[tag] = [existing, child_value]
        else:
            result[tag] = child_value
    return result


def xml_to_dict(xml_string: str) -> dict[str, Any]:
    """Parse an XML string and return a dict representation.

    The root element becomes the single top-level key.  Repeated sibling
    elements with the same tag name are collected into a list.
    """
    root = ET.fromstring(xml_string)
    return {root.tag: _element_to_dict(root)}
