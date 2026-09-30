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
"""Shared Pydantic schema generation for one OpenAPI document."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import PydanticUserError, TypeAdapter
from pydantic.json_schema import CoreModeRef, CoreRef, DefsRef, GenerateJsonSchema, JsonSchemaMode

from pyfly.web.openapi_metadata import UNSET

# Pydantic appends metadata reprs to core references. Quoted constraint strings
# can themselves contain colons/digits, so only unquoted identity suffixes count.
_REFERENCE_TOKEN = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|(?P<identity>:(?:str-)?[0-9]+)(?=$|[\[,\]_])")


class _StableDefinitionNames(GenerateJsonSchema):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._reference_ids: dict[str, int] = {}
        super().__init__(*args, **kwargs)

    def get_defs_ref(self, core_mode_ref: CoreModeRef) -> DefsRef:
        """Replace process-local identities only at the definition-naming boundary.

        Pydantic's rsplit(':', 1) can retain an identity when a metadata pattern
        contains colons. Stable encounter indices preserve distinct same-name types
        while leaving the original core references, schema data and cache keys intact.
        """
        core_ref, mode = core_mode_ref

        def replace(match: re.Match[str]) -> str:
            identity = match.group("identity")
            if identity is None:
                return match.group()
            index = self._reference_ids.setdefault(identity, len(self._reference_ids) + 1)
            prefix = ":str-" if identity.startswith(":str-") else ":"
            return f"{prefix}{index}"

        return super().get_defs_ref((CoreRef(_REFERENCE_TOKEN.sub(replace, core_ref)), mode))


@dataclass
class _SchemaSlot:
    key: int
    mode: JsonSchemaMode
    default: Any = UNSET


class SchemaRegistry:
    """Defer schemas until every input is known, so Pydantic resolves name collisions."""

    def __init__(self, schema_generator: type[GenerateJsonSchema]) -> None:
        # The consumer stays in the MRO, including its naming and schema hooks.
        self._generator = type("_OpenAPISchemaGenerator", (_StableDefinitionNames, schema_generator), {})
        self._inputs: list[tuple[int, JsonSchemaMode, TypeAdapter[Any]]] = []

    def schema(
        self, schema_type: Any, mode: JsonSchemaMode, *, context: str, explicit: bool = True, default: Any = UNSET
    ) -> _SchemaSlot | None:
        try:
            adapter = schema_type if isinstance(schema_type, TypeAdapter) else TypeAdapter(schema_type)
            # Validate separately to provide the responsible route for bad explicit inputs,
            # and preserve the undocumented fallback for unsupported runtime response types.
            adapter.json_schema(mode=mode, schema_generator=self._generator)
        except (PydanticUserError, TypeError, ValueError) as exc:
            if not explicit:
                return None
            raise ValueError(f"Invalid OpenAPI schema for {context}: {exc}") from exc
        if default is not UNSET:
            try:
                default = adapter.dump_python(default, mode="json")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid OpenAPI schema default for {context}: {exc}") from exc
        key = len(self._inputs)
        self._inputs.append((key, mode, adapter))
        return _SchemaSlot(key, mode, default)

    def resolve(self, document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        schemas, definitions = TypeAdapter.json_schemas(
            self._inputs, by_alias=True, ref_template="#/components/schemas/{model}", schema_generator=self._generator
        )

        def replace(value: Any) -> Any:
            if isinstance(value, _SchemaSlot):
                schema = dict(schemas[value.key, value.mode])
                if value.default is not UNSET:
                    schema["default"] = value.default
                return schema
            if isinstance(value, dict):
                return {key: replace(child) for key, child in value.items()}
            if isinstance(value, list):
                return [replace(child) for child in value]
            return value

        return replace(document), definitions.get("$defs", {})
