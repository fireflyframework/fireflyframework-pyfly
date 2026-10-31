# Copyright 2026 Firefly Software Foundation
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

"""The recursive, explicitly admitted skill configuration schema subset."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pyfly.agentic.skills import _values

_TYPES = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (tuple,),
    "object": (Mapping,),
    "null": (type(None),),
}
_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "default",
    "title",
    "description",
}


def _type_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value in _TYPES:
        return (value,)
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(name, str) and name in _TYPES for name in value)
        and len(set(value)) == 2
        and "null" in value
    ):
        return value
    raise ValueError("schema type must name a supported JSON type or one type plus null")


def check_schema(schema: Mapping[str, Any]) -> None:
    unknown = set(schema) - _KEYS
    if unknown:
        raise ValueError(f"unsupported schema keywords: {sorted(unknown)}")
    if "type" in schema:
        _type_names(schema["type"])
    for key in ("title", "description"):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError(f"schema {key} must be a string")
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            value = schema[key]
            if type(value) not in (int, float) or (
                key not in ("minimum", "maximum") and (type(value) is not int or value < 0)
            ):
                raise ValueError(f"invalid schema {key}")
    for lower, upper in (("minimum", "maximum"), ("minLength", "maxLength"), ("minItems", "maxItems")):
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise ValueError(f"schema {lower} exceeds {upper}")
    if "enum" in schema and (not isinstance(schema["enum"], tuple) or not schema["enum"]):
        raise ValueError("schema enum must be a non-empty array")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError("schema additionalProperties must be boolean")
    required = schema.get("required", ())
    if not isinstance(required, tuple) or any(not isinstance(key, str) for key in required):
        raise ValueError("schema required must be an array of names")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("schema properties must be an object")
    if len(set(required)) != len(required) or any(name not in properties for name in required):
        raise ValueError("schema required names must be unique and declared in properties")
    for child in properties.values():
        if not isinstance(child, Mapping):
            raise ValueError("property schemas must be objects")
        check_schema(child)
    if "items" in schema:
        if not isinstance(schema["items"], Mapping):
            raise ValueError("schema items must be one schema object")
        check_schema(schema["items"])
    if "default" in schema and validate(schema["default"], schema):
        raise ValueError("schema default does not satisfy its schema")


def defaults(value: Any, schema: Mapping[str, Any]) -> Any:
    remaining = _values.MAX_NODES
    size = 0

    def add_bytes(count: int) -> None:
        nonlocal size
        size += count
        if size > _values.MAX_JSON_BYTES:
            raise ValueError("default expansion exceeds JSON byte limit")

    def visit(item: Any, rule: Mapping[str, Any], depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > _values.MAX_DEPTH:
            raise ValueError("default expansion exceeds JSON node or depth limit")
        if isinstance(item, Mapping):
            add_bytes(2)
            result: dict[str, Any] = {}
            properties = rule.get("properties", {})

            def insert(key: str, child: Any, child_rule: Mapping[str, Any]) -> None:
                add_bytes(len(_values.canonical(key)) + 1 + bool(result))
                result[key] = visit(child, child_rule, depth + 1)

            for key, child in item.items():
                insert(key, child, properties.get(key, {}))
            for key, child_rule in properties.items():
                if key not in item and "default" in child_rule:
                    insert(key, child_rule["default"], child_rule)
            return result
        if isinstance(item, tuple):
            add_bytes(2 + max(0, len(item) - 1))
            return tuple(visit(child, rule.get("items", {}), depth + 1) for child in item)
        add_bytes(len(_values.canonical(item)))
        return item

    return visit(value, schema, 0)


def validate(value: Any, schema: Mapping[str, Any], path: str = "") -> list[str]:
    problems: list[str] = []

    def problem(message: str) -> None:
        if len(problems) < 128:
            problems.append(f"{path} {message}".strip())

    expected = schema.get("type")
    if expected and not any(
        isinstance(value, _TYPES[name]) and not (name in ("number", "integer") and isinstance(value, bool))
        for name in _type_names(expected)
    ):
        problem(f"must be a {expected}, got {type(value).__name__}")
        return problems
    if "enum" in schema and not any(_equal(value, member) for member in schema["enum"]):
        problem("must be one of the schema enum values")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problem(f"must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problem(f"must be <= {schema['maximum']}")
    for kind, lower, upper in ((str, "minLength", "maxLength"), (tuple, "minItems", "maxItems")):
        if isinstance(value, kind):
            if lower in schema and len(value) < schema[lower]:
                problem(f"must have at least {schema[lower]} items/characters")
            if upper in schema and len(value) > schema[upper]:
                problem(f"must have at most {schema[upper]} items/characters")
    if isinstance(value, Mapping):
        for key in schema.get("required", ()):
            if key not in value:
                problem(f"{key} is required")
        properties = schema.get("properties", {})
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in properties:
                problems.extend(validate(child, properties[key], child_path)[: 128 - len(problems)])
            elif schema.get("additionalProperties") is False and len(problems) < 128:
                problems.append(f"{child_path} is unexpected (not in the schema)")
    if isinstance(value, tuple) and "items" in schema:
        for index, child in enumerate(value):
            problems.extend(validate(child, schema["items"], f"{path}[{index}]")[: 128 - len(problems)])
    return problems


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)
