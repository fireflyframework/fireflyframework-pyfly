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
"""Map logback-style layout patterns to Python logging format strings."""

from __future__ import annotations

import re

# Order matters: longer aliases before their single-letter forms.
_TOKENS: list[tuple[str, str]] = [
    ("%logger", "%(name)s"),
    ("%level", "%(levelname)s"),
    ("%message", "%(message)s"),
    ("%thread", "%(threadName)s"),
    ("%msg", "%(message)s"),
    ("%c", "%(name)s"),
    ("%p", "%(levelname)s"),
    ("%m", "%(message)s"),
    ("%t", "%(threadName)s"),
    ("%n", "\n"),
]

_DATE_RE = re.compile(r"%d(?:\{([^}]*)\})?")
_TRUNC_RE = re.compile(r"(%(?:logger|c))\{\d+\}")


def compile_pattern(spec: str) -> tuple[str, str | None]:
    """Return ``(python_format, datefmt)`` for a logback-style *spec*.

    Recognises ``%d{fmt}`` (timestamp), ``%p``/``%level``, ``%c``/``%logger``
    (with ``{N}`` truncation accepted but ignored), ``%m``/``%msg``/``%message``,
    ``%t``/``%thread``, and ``%n``. Unknown text passes through unchanged.
    """
    datefmt: str | None = None

    def _date(match: re.Match[str]) -> str:
        nonlocal datefmt
        datefmt = match.group(1) or None
        return "%(asctime)s"

    result = _DATE_RE.sub(_date, spec)
    result = _TRUNC_RE.sub(r"\1", result)  # drop {N} truncation suffixes
    for token, replacement in _TOKENS:
        result = result.replace(token, replacement)
    return result, datefmt
