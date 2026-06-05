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
"""Opt-in stdout/stderr redaction wrapper."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from pyfly.logging.redaction.engine import Redactor


class RedactingTextIO:
    """Line-buffered text wrapper that redacts PII before writing through.

    Partial lines are buffered until a newline (or ``flush``) so multi-write
    PII isn't split across redaction boundaries.
    """

    def __init__(self, wrapped: TextIO, redactor: Redactor) -> None:
        self._wrapped = wrapped
        self._redactor = redactor
        self._buffer = ""

    def write(self, data: str) -> int:
        if not isinstance(data, str):
            return self._wrapped.write(data)
        self._buffer += data
        written = len(data)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._wrapped.write(self._redactor.redact(line) + "\n")
        return written

    def flush(self) -> None:
        if self._buffer:
            self._wrapped.write(self._redactor.redact(self._buffer))
            self._buffer = ""
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        # Delegate isatty/fileno/encoding/etc. to the wrapped stream.
        return getattr(self._wrapped, name)


def install_stream_redaction(redactor: Redactor) -> Callable[[], None]:
    """Wrap sys.stdout/sys.stderr; returns a restore callable."""
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout = RedactingTextIO(original_out, redactor)
    sys.stderr = RedactingTextIO(original_err, redactor)

    def restore() -> None:
        sys.stdout = original_out
        sys.stderr = original_err

    return restore
