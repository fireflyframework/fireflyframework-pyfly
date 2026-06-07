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
"""HTTP message converters — the Spring ``HttpMessageConverter`` equivalent.

An ordered, pluggable registry of converters, each bound to media types, used for
BOTH reading request bodies and writing responses. Content negotiation honors the
``Accept`` header with q-values on write and the ``Content-Type`` on read. Ships
JSON and XML converters; register more (e.g. CBOR) via a ``MessageConverterRegistry``
bean. JSON (de)serialization goes through :class:`~pyfly.web.json.PyFlyJsonSerializer`,
so global ``pyfly.web.json.*`` config applies to every format.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from pyfly.web.converters import dict_to_xml, xml_to_dict
from pyfly.web.json import PyFlyJsonSerializer

# Cache of extra='forbid' overlays so fail-on-unknown rejects unknown body keys
# without mutating the user's model class.
_strict_overlays: dict[type, type] = {}


def _strict_model(model: type[BaseModel]) -> type[BaseModel]:
    overlay = _strict_overlays.get(model)
    if overlay is None:
        overlay = type(f"{model.__name__}__Strict", (model,), {"model_config": ConfigDict(extra="forbid")})
        _strict_overlays[model] = overlay
    return overlay


def parse_accept(accept: str | None) -> list[str]:
    """Parse an ``Accept`` header into media types ordered by descending q-value."""
    if not accept:
        return ["application/json"]
    items: list[tuple[str, float]] = []
    for index, part in enumerate(accept.split(",")):
        part = part.strip()
        if not part:
            continue
        tokens = part.split(";")
        media_type = tokens[0].strip().lower()
        quality = 1.0
        for token in tokens[1:]:
            token = token.strip()
            if token.startswith("q="):
                try:
                    quality = float(token[2:])
                except ValueError:
                    quality = 1.0
        # Stable within equal q: preserve header order via the index tiebreak.
        items.append((media_type, quality - index * 1e-6))
    items.sort(key=lambda pair: pair[1], reverse=True)
    return [media_type for media_type, _ in items]


class MessageConverter:
    """Base converter: reads/writes HTTP bodies for its :attr:`media_types`."""

    media_types: tuple[str, ...] = ()

    def supports(self, media_type: str | None) -> bool:
        """Whether this converter handles *media_type* (``*/*`` matches any)."""
        if media_type is None:
            return False
        base = media_type.split(";", 1)[0].strip().lower()
        return base == "*/*" or base in self.media_types

    def read(self, body: bytes, target_type: type) -> Any:
        raise NotImplementedError

    def write(self, value: Any) -> tuple[bytes, str]:
        """Serialize *value*; returns ``(body_bytes, content_type)``."""
        raise NotImplementedError


class JsonMessageConverter(MessageConverter):
    """JSON via :class:`PyFlyJsonSerializer` (global config) + Pydantic validation."""

    media_types = ("application/json",)

    def __init__(self, serializer: PyFlyJsonSerializer | None = None, *, fail_on_unknown: bool = False) -> None:
        self._serializer = serializer or PyFlyJsonSerializer()
        self._fail_on_unknown = fail_on_unknown

    def read(self, body: bytes, target_type: type) -> Any:
        if isinstance(target_type, type) and issubclass(target_type, BaseModel):
            model = _strict_model(target_type) if self._fail_on_unknown else target_type
            return model.model_validate_json(body)
        return json.loads(body.decode() or "null")

    def write(self, value: Any) -> tuple[bytes, str]:
        data = self._serializer.to_response_data(value)
        return json.dumps(data).encode("utf-8"), "application/json"


class XmlMessageConverter(MessageConverter):
    """XML via stdlib ElementTree (read AND write), normalized through the serializer."""

    media_types = ("application/xml", "text/xml")

    def __init__(self, serializer: PyFlyJsonSerializer | None = None) -> None:
        self._serializer = serializer or PyFlyJsonSerializer()

    def read(self, body: bytes, target_type: type) -> Any:
        parsed = xml_to_dict(body.decode())
        inner = next(iter(parsed.values())) if len(parsed) == 1 else parsed  # unwrap the root tag
        if isinstance(target_type, type) and issubclass(target_type, BaseModel):
            return target_type.model_validate(inner)
        return inner

    def write(self, value: Any) -> tuple[bytes, str]:
        data = self._serializer.to_response_data(value)
        return dict_to_xml(data).encode("utf-8"), "application/xml"


class MessageConverterRegistry:
    """Ordered converters; first match wins. Reads by ``Content-Type``, writes by
    ``Accept`` (q-value ordered). User-added converters take priority."""

    def __init__(self, converters: list[MessageConverter] | None = None) -> None:
        self._converters: list[MessageConverter] = list(converters or [])

    def add(self, converter: MessageConverter) -> None:
        """Register *converter* at the front (highest priority)."""
        self._converters.insert(0, converter)

    @property
    def converters(self) -> list[MessageConverter]:
        return list(self._converters)

    def find_reader(self, content_type: str | None) -> MessageConverter | None:
        """The converter for a request ``Content-Type`` (falls back to the first)."""
        for converter in self._converters:
            if content_type is not None and converter.supports(content_type):
                return converter
        return self._converters[0] if self._converters else None

    def find_writer(self, accept: str | None) -> MessageConverter | None:
        """The best converter for an ``Accept`` header (q-value ordered)."""
        for media_type in parse_accept(accept):
            for converter in self._converters:
                if converter.supports(media_type):
                    return converter
        return self._converters[0] if self._converters else None


def default_message_converters(
    serializer: PyFlyJsonSerializer | None = None,
    *,
    fail_on_unknown: bool = False,
) -> MessageConverterRegistry:
    """The built-in registry: JSON (first/default) then XML, sharing *serializer*."""
    serializer = serializer or PyFlyJsonSerializer()
    return MessageConverterRegistry(
        [
            JsonMessageConverter(serializer, fail_on_unknown=fail_on_unknown),
            XmlMessageConverter(serializer),
        ]
    )


def install_serialization_state(app: Any, context: Any) -> None:
    """Wire the JSON serializer, message-converter registry, and RFC 7807 flag onto
    ``app.state`` from config — shared by the Starlette AND FastAPI adapters so the two
    cannot drift (the FastAPI adapter previously skipped this entirely).

    Sets ``app.state.pyfly_json_serializer`` / ``pyfly_message_converters`` /
    ``pyfly_problem_details``. A user ``JsonSerializers`` or ``MessageConverterRegistry``
    bean overrides the defaults; with no context, lenient defaults are used.
    """
    from pyfly.web.json import JsonSerializers, PyFlyJsonSerializer, json_properties_from_config

    props = json_properties_from_config(context.config) if context is not None else None
    registry = JsonSerializers()
    if context is not None:
        try:
            registry = context.get_bean(JsonSerializers)
        except Exception:  # noqa: BLE001 - registry bean is optional; default when absent
            registry = JsonSerializers()
    serializer = PyFlyJsonSerializer(props, registry)
    app.state.pyfly_json_serializer = serializer

    fail_on_unknown = props.fail_on_unknown_properties if props is not None else False
    converters = default_message_converters(serializer, fail_on_unknown=fail_on_unknown)
    if context is not None:
        try:
            override = context.get_bean(MessageConverterRegistry)
        except Exception:  # noqa: BLE001 - override bean is optional; default when absent
            override = None
        if override is not None:
            converters = override
    app.state.pyfly_message_converters = converters

    app.state.pyfly_problem_details = (
        str(context.config.get("pyfly.web.problem-details.enabled", "false")).lower() in ("true", "1", "yes")
        if context is not None
        else False
    )
