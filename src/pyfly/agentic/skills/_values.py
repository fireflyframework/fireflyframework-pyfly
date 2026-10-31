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

"""Bounded, immutable JSON values shared by skill authoring and rendering."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

MAX_TEXT = 65_536
MAX_JSON_BYTES = 262_144
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_RESOURCE_BYTES = 1_048_576
MAX_COLLECTION = 128


def text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"text must be a string of at most {limit} characters")
    return value


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    return value


def canonical(value: Any) -> str:
    return json.dumps(plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def freeze(value: Any) -> Any:
    remaining = MAX_NODES
    size = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining, size
        remaining -= 1
        if depth > MAX_DEPTH or remaining < 0:
            raise ValueError(f"JSON values exceed depth {MAX_DEPTH} or {MAX_NODES} nodes")
        if isinstance(item, Mapping):
            result = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                text(key)
                size += len(key)
                if size > MAX_JSON_BYTES:
                    raise ValueError(f"JSON values exceed {MAX_JSON_BYTES} bytes")
                result[key] = visit(child, depth + 1)
            return MappingProxyType(result)
        if isinstance(item, (list, tuple)):
            return tuple(visit(child, depth + 1) for child in item)
        if isinstance(item, str):
            text(item)
            size += len(item)
        elif item is None or isinstance(item, bool):
            pass
        elif isinstance(item, int):
            if item.bit_length() > 4096:
                raise ValueError("JSON integers must fit within 4096 bits")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
        else:
            raise ValueError("skill values must be JSON data")
        if size > MAX_JSON_BYTES:
            raise ValueError(f"JSON values exceed {MAX_JSON_BYTES} bytes")
        return item

    frozen = visit(value, 0)
    if len(canonical(frozen).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"JSON values exceed {MAX_JSON_BYTES} bytes")
    return frozen


def mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("expected a JSON object")
    result: Mapping[str, Any] = freeze({} if value is None else value)
    return result


def resource_text(value: Any) -> str:
    result = text(value, MAX_RESOURCE_BYTES)
    if len(result.encode("utf-8")) > MAX_RESOURCE_BYTES:
        raise ValueError(f"resource exceeds {MAX_RESOURCE_BYTES} UTF-8 bytes")
    return result
