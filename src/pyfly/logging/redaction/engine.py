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
"""Redactor engines: fast regex (default) + optional Presidio."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyfly.logging.redaction.patterns import BUILTIN_PATTERNS, VALIDATORS

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


class RegexRedactor:
    """Pattern-based redactor — fast, no external dependencies (default)."""

    def __init__(
        self,
        entities: list[str],
        mask: str = "placeholder",
        extra_patterns: dict[str, str] | None = None,
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

    def redact(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
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


class PresidioRedactor:
    """Microsoft Presidio (NER) redactor — used when the pyfly[pii] extra is installed."""

    def __init__(self, props: RedactionProperties) -> None:
        from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found, unused-ignore]
        from presidio_anonymizer import AnonymizerEngine  # type: ignore[import-not-found, unused-ignore]

        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()
        self._language = (props.presidio.languages or ["en"])[0]
        self._threshold = props.presidio.score_threshold
        self._entities = props.entities or None
        self._fallback = RegexRedactor(props.entities, props.mask, props.extra_patterns)

    def redact(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        try:
            results = self._analyzer.analyze(text=text, language=self._language, entities=self._entities)
            results = [r for r in results if r.score >= self._threshold]
            if not results:
                return self._fallback.redact(text)
            return self._anonymizer.anonymize(text=text, analyzer_results=results).text
        except Exception:  # noqa: BLE001 — never let redaction crash logging
            return self._fallback.redact(text)


def build_redactor(props: RedactionProperties) -> Redactor | None:
    """Resolve the configured redactor, or None when redaction is disabled."""
    if not props.enabled:
        return None
    engine = props.engine.lower().strip()
    if engine == "presidio":
        return PresidioRedactor(props)
    if engine == "auto":
        try:
            import presidio_analyzer  # type: ignore[import-not-found, unused-ignore] # noqa: F401

            return PresidioRedactor(props)
        except ImportError:
            _logger.info("presidio not installed; using regex PII redaction (install pyfly[pii] to upgrade)")
    return RegexRedactor(props.entities, props.mask, props.extra_patterns)
