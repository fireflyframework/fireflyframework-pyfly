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
"""Native pattern-based redaction for logging."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyfly.logging.redaction.patterns import BUILTIN_PATTERNS, PRESERVE_PATTERNS, VALIDATORS

if TYPE_CHECKING:
    from pyfly.config.properties.logging import RedactionProperties

_logger = logging.getLogger("pyfly.logging")


@runtime_checkable
class Redactor(Protocol):
    """Masks PII in a string. Implementations must be side-effect free."""

    def redact(self, text: Any) -> Any: ...


def _mask(value: str, entity: str, style: str) -> str:
    if style == "partial":
        keep = value[-4:] if len(value) > 4 else ""
        return f"{'*' * max(0, len(value) - len(keep))}{keep}"
    if style == "hash":
        digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
        return f"<{entity}:{digest}>"
    return f"<{entity}>"


def compile_preserve_patterns(extra: dict[str, str] | None = None) -> list[re.Pattern[str]]:
    """The identifier shapes no entity pattern may touch, built-ins first.

    Each pattern is kept separate rather than joined into one alternation, so a
    consumer-supplied regex with named groups or backreferences cannot break the
    others. An invalid one is warned about and dropped, exactly as an invalid
    ``extra-patterns`` entry is.
    """
    patterns = list(PRESERVE_PATTERNS.values())
    for name, regex in (extra or {}).items():
        try:
            patterns.append(re.compile(regex))
        except re.error:
            _logger.warning("Ignoring invalid redaction preserve pattern for %s", name)
    return patterns


def protected_spans(text: str, patterns: list[re.Pattern[str]]) -> list[tuple[int, int]]:
    """Merged, ordered ``(start, end)`` ranges of *text* that must survive redaction."""
    found: list[tuple[int, int]] = []
    for pattern in patterns:
        found.extend((m.start(), m.end()) for m in pattern.finditer(text) if m.end() > m.start())
    if not found:
        return found
    found.sort()
    merged: list[tuple[int, int]] = [found[0]]
    for start, end in found[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _redact_around_protected(text: str, patterns: list[re.Pattern[str]], redact_gap: Callable[[str], str]) -> str:
    """Apply *redact_gap* to everything in *text* except the protected spans."""
    spans = protected_spans(text, patterns)
    if not spans:
        return redact_gap(text)
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(redact_gap(text[cursor:start]))
        parts.append(text[start:end])
        cursor = end
    parts.append(redact_gap(text[cursor:]))
    return "".join(parts)


class RegexRedactor:
    """Pattern-based redactor — fast, no external dependencies (default)."""

    def __init__(
        self,
        entities: list[str],
        mask: str = "placeholder",
        extra_patterns: dict[str, str] | None = None,
        preserve_patterns: dict[str, str] | None = None,
    ) -> None:
        self._mask = mask
        self._rules: list[tuple[str, re.Pattern[str], Callable[[str], bool] | None]] = []
        for ent in entities:
            pat = BUILTIN_PATTERNS.get(ent)
            if pat is not None:
                self._rules.append((ent, pat, VALIDATORS.get(ent)))
        for name, regex in (extra_patterns or {}).items():
            try:
                self._rules.append((name, re.compile(regex), None))
            except re.error:
                _logger.warning("Ignoring invalid redaction pattern for %s", name)
        self._preserve = compile_preserve_patterns(preserve_patterns)

    def redact(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        # Identifier shapes are carved out FIRST and the entity rules only ever
        # see the gaps between them. Splitting the text is what makes the
        # protection absolute: a pattern cannot match across a protected span,
        # so no rule can consume half a uuid.
        return _redact_around_protected(text, self._preserve, self._redact_entities)

    def _redact_entities(self, text: str) -> str:
        if not text:
            return text
        result = text
        for entity, pattern, validator in self._rules:

            def _sub(match: re.Match[str], _e: str = entity, _v: Callable[[str], bool] | None = validator) -> str:
                token = match.group(0)
                if _v is not None and not _v(token):
                    return token
                return _mask(token, _e, self._mask)

            result = pattern.sub(_sub, result)
        return result


def build_redactor(props: RedactionProperties) -> Redactor | None:
    """Resolve the configured redactor, or None when redaction is disabled."""
    if not props.enabled:
        return None
    engine = props.engine.lower().strip()
    if engine not in ("regex", "auto"):
        raise ValueError(
            "pyfly.logging.redaction.engine supports regex (or auto as a regex alias); "
            "remove obsolete NLP engine settings and use regex"
        )
    return RegexRedactor(props.entities, props.mask, props.extra_patterns, props.preserve_patterns)
