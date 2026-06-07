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
"""Central JSON (de)serialization layer — the pyfly-idiomatic ObjectMapper equivalent.

Pydantic remains the per-model engine (``Field(alias=...)``, ``@field_serializer``,
discriminated unions, etc.). This module adds what Spring centralizes and pyfly
lacked: **global** JSON config (``pyfly.web.json.*``) applied at one serialization
boundary, and a registry for serializing **non-Pydantic** arbitrary types.

It is deliberately NOT a Jackson clone — no ``@JsonView``, no Modules SPI, no
``ObjectMapper`` god-object, no codegen, no global ``alias_generator`` injected into
user models (use the opt-in :class:`CamelModel` base instead).
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Opt-in base model with camelCase JSON I/O (also accepts snake_case input)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class JsonProperties(BaseModel):
    """Global JSON (de)serialization configuration, bound from ``pyfly.web.json.*``."""

    property_naming_strategy: Literal["as-is", "camelCase"] = "as-is"
    by_alias: bool = False
    exclude_none: bool = False
    exclude_defaults: bool = False
    fail_on_unknown_properties: bool = False

    def effective_by_alias(self) -> bool:
        """Whether to dump using field aliases (camelCase strategy implies True)."""
        return self.by_alias or self.property_naming_strategy == "camelCase"


class JsonSerializers:
    """Registry of encoders for **non-Pydantic** arbitrary types (e.g. a Money value object).

    Register globally (typically via a DI bean) so the type serializes consistently
    everywhere::

        serializers.register(Money, encode=lambda m: {"amount": str(m.amount), "ccy": m.currency})
    """

    def __init__(self) -> None:
        self._encoders: dict[type, Callable[[Any], Any]] = {}

    def register(self, tp: type, *, encode: Callable[[Any], Any]) -> None:
        """Register an encoder turning *tp* instances into a JSON-safe value."""
        self._encoders[tp] = encode

    def encode_for(self, tp: type) -> Callable[[Any], Any] | None:
        """The registered encoder for *tp* (honoring inheritance), or ``None``."""
        for base in getattr(tp, "__mro__", (tp,)):
            if base in self._encoders:
                return self._encoders[base]
        return None


def _encode_builtin(value: Any) -> Any:
    """Encode common stdlib types Pydantic handles inside models but ``json`` does not."""
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)  # match Pydantic mode="json" (string, no float precision loss)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, set | frozenset):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class PyFlyJsonSerializer:
    """Applies global :class:`JsonProperties` + the :class:`JsonSerializers` registry
    at the one serialization boundary, and normalizes arbitrary nested structures into
    JSON-safe values (so a list of dicts, a mixed list, or a dict containing models no
    longer breaks ``json.dumps``).
    """

    def __init__(self, properties: JsonProperties | None = None, registry: JsonSerializers | None = None) -> None:
        self._props = properties or JsonProperties()
        self._registry = registry or JsonSerializers()

    @property
    def properties(self) -> JsonProperties:
        return self._props

    def to_response_data(self, result: Any) -> Any:
        """Normalize a handler result into a fully JSON-safe value."""
        return self._normalize(result)

    def _dump_model(self, model: BaseModel) -> Any:
        return model.model_dump(
            mode="json",
            by_alias=self._props.effective_by_alias(),
            exclude_none=self._props.exclude_none,
            exclude_defaults=self._props.exclude_defaults,
        )

    def _normalize(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            return self._dump_model(value)
        if isinstance(value, dict):
            return {k: self._normalize(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self._normalize(v) for v in value]
        if value is None or isinstance(value, str | int | float | bool):
            return value
        return self.encode_default(value)

    def encode_default(self, value: Any) -> Any:
        """Encode a non-Pydantic, non-primitive value (registry → dataclass → builtins)."""
        encoder = self._registry.encode_for(type(value))
        if encoder is not None:
            return encoder(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._normalize(dataclasses.asdict(value))
        return _encode_builtin(value)


def json_properties_from_config(config: Any) -> JsonProperties:
    """Build :class:`JsonProperties` from a pyfly ``Config`` (``pyfly.web.json.*``)."""

    def _flag(key: str) -> bool:
        return str(config.get(f"pyfly.web.json.{key}", "false")).lower() in ("true", "1", "yes")

    strategy = str(config.get("pyfly.web.json.property-naming-strategy", "as-is"))
    return JsonProperties(
        property_naming_strategy="camelCase" if strategy == "camelCase" else "as-is",
        by_alias=_flag("by-alias"),
        exclude_none=_flag("exclude-none"),
        exclude_defaults=_flag("exclude-defaults"),
        fail_on_unknown_properties=_flag("fail-on-unknown-properties"),
    )
