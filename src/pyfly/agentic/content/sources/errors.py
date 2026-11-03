# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Safe provider failures without remote URLs, bodies or credential material."""

from typing import Literal

ErrorCode = Literal[
    "invalid_response",
    "invalid_cursor",
    "access_denied",
    "reconnect_required",
    "unavailable",
    "resync_required",
    "throttled",
    "transport_error",
    "timeout",
    "unsupported",
    "size_exceeded",
    "version_changed",
    "unsafe_url",
    "scope_mismatch",
]


class SourceError(Exception):
    def __init__(self, code: ErrorCode, *, retry_after: int | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)
