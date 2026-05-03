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
"""Locale resolution — protocol and built-in resolvers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LocaleResolver(Protocol):
    """Port for determining the locale from an incoming request."""

    def resolve_locale(self, request: Any) -> str: ...


class AcceptHeaderLocaleResolver:
    """Parses the ``Accept-Language`` header and returns the best match.

    The resolver picks the language tag with the highest quality value.
    When no header is present or parsing fails it falls back to
    *default_locale*.
    """

    def __init__(self, default_locale: str = "en") -> None:
        self._default = default_locale

    def resolve_locale(self, request: Any) -> str:
        header: str = getattr(request, "accept_language", "") or ""
        if not header:
            headers = getattr(request, "headers", None)
            if headers is not None:
                header = headers.get("accept-language", "")

        if not header:
            return self._default

        return _parse_accept_language(header, self._default)


class FixedLocaleResolver:
    """Always returns a pre-configured locale, ignoring the request."""

    def __init__(self, locale: str = "en") -> None:
        self._locale = locale

    def resolve_locale(self, request: Any) -> str:  # noqa: ARG002
        return self._locale


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_accept_language(header: str, default: str) -> str:
    """Return the language tag with the highest *q* value from *header*.

    Handles the standard ``Accept-Language`` format, e.g.
    ``en-US,en;q=0.9,fr;q=0.8``.
    """
    best_locale = default
    best_quality = 0.0

    for part in header.split(","):
        part = part.strip()
        if not part:
            continue

        if ";q=" in part:
            tag, _, q_str = part.partition(";q=")
            try:
                quality = float(q_str.strip())
            except ValueError:
                continue
        elif ";Q=" in part:
            tag, _, q_str = part.partition(";Q=")
            try:
                quality = float(q_str.strip())
            except ValueError:
                continue
        else:
            tag = part
            quality = 1.0

        tag = tag.strip()
        if quality > best_quality:
            best_quality = quality
            best_locale = tag.split("-")[0].lower()

    return best_locale
