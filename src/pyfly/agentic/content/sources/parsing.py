# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Bounded provider response fields, without echoing malformed input."""

from typing import Any
from urllib.parse import quote

from pyfly.agentic.content.sources.dtos import Cursor, SourceScope, bounded
from pyfly.agentic.content.sources.errors import SourceError


def text(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    try:
        return bounded(value)
    except (ValueError, TypeError):
        raise SourceError("invalid_response") from None


def identifier(value: Any) -> str:
    result = text(value)
    assert result is not None
    return result


def rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 200 or any(not isinstance(v, dict) for v in value):
        raise SourceError("invalid_response")
    return value


def size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) < 20:
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise SourceError("invalid_response")
    return value


def resume(cursor: Cursor | None, scope: SourceScope, operation: str) -> str | None:
    if cursor is None:
        return None
    if cursor.scope != scope or cursor.operation != operation:
        raise SourceError("invalid_cursor")
    return cursor.token


def path_id(value: str) -> str:
    value = identifier(value)
    if value in {".", ".."} or any(c in value for c in "/\\%?#"):
        raise SourceError("invalid_response")
    return quote(value, safe="")
