# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Exact-built-in data copies; host objects never enter persistent guests."""

from __future__ import annotations

import math
from typing import cast

from pyfly.agentic.execution.session_types import ExecutionValue


def copy_value(value: object, max_bytes: int) -> ExecutionValue:
    """Copy an entire boundary payload with a deterministic content-size meter.

    Charge 8 per value, UTF-8/raw/integer bytes, 8 per sequence member and 16
    per mapping entry. This is not a measurement of host heap/wire memory.
    """
    remaining = max_bytes
    active: set[int] = set()

    def charge(size: int) -> None:
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise ValueError("Execution value exceeds max_value_bytes")

    def visit(item: object, depth: int) -> ExecutionValue:
        if depth > 100:
            raise ValueError("Execution value exceeds maximum nesting depth")
        charge(8)
        kind = type(item)
        if item is None or kind is bool:
            return cast(ExecutionValue, item)
        if kind is int:
            charge((cast(int, item).bit_length() + 7) // 8)
            return cast(int, item)
        if kind is float:
            if not math.isfinite(cast(float, item)):
                raise ValueError("Execution values require finite floats")
            return cast(float, item)
        if kind is str:
            charge(len(cast(str, item).encode("utf-8")))
            return cast(str, item)
        if kind is bytes:
            charge(len(cast(bytes, item)))
            return cast(bytes, item)
        if kind not in (list, tuple, dict):
            raise ValueError("Execution values must contain exact built-in data types only")
        identity = id(item)
        if identity in active:
            raise ValueError("Execution values cannot contain cycles")
        active.add(identity)
        try:
            if kind is dict:
                result: dict[str, ExecutionValue] = {}
                for key, child in cast(dict[object, object], item).items():
                    if type(key) is not str:
                        raise ValueError("Execution mappings require exact string keys")
                    text = key
                    charge(16 + len(text.encode("utf-8")))
                    result[text] = visit(child, depth + 1)
                return result
            sequence: list[ExecutionValue] = []
            for child in cast(list[object] | tuple[object, ...], item):
                charge(8)
                sequence.append(visit(child, depth + 1))
            return tuple(sequence) if kind is tuple else sequence
        finally:
            active.remove(identity)

    return visit(value, 0)
