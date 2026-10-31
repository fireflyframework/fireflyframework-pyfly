# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Length-prefixed, bounded JSON framing for an untrusted guest."""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

from pyfly.agentic.sandbox.contracts import encode_json


class FrameError(ValueError):
    """A frame is invalid, truncated, duplicated, or exceeds a bound."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError("Duplicate JSON member")
        result[key] = value
    return result


async def read_frame(reader: asyncio.StreamReader, *, limit: int) -> Any:
    try:
        header = await reader.readexactly(4)
        size = struct.unpack(">I", header)[0]
        if not 0 < size <= limit:
            raise FrameError("Invalid frame size")
        raw = await reader.readexactly(size)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda _: (_ for _ in ()).throw(FrameError("Invalid number")),
        )
        encode_json(value, limit=limit)
        return value
    except (
        asyncio.IncompleteReadError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        TypeError,
    ) as exc:
        raise FrameError("Invalid frame") from exc


async def write_frame(writer: Any, value: Any, *, limit: int) -> None:
    try:
        data = encode_json(value, limit=limit)
    except (ValueError, TypeError, RecursionError) as exc:
        raise FrameError("Invalid frame") from exc
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()
