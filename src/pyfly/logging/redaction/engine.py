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
        from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found, import-untyped, unused-ignore]
        from presidio_analyzer.nlp_engine import (  # type: ignore[import-not-found, import-untyped, unused-ignore]
            NlpEngineProvider,
        )
        from presidio_anonymizer import (  # type: ignore[import-not-found, import-untyped, unused-ignore]
            AnonymizerEngine,
        )

        language = (props.presidio.languages or ["en"])[0]
        # Build the NLP engine for the configured spaCy model (default
        # en_core_web_lg) so the model is explicit and swappable (e.g. CI uses the
        # tiny en_core_web_sm). Typed as Any so redact() type-checks independently
        # of presidio's own type surface; the no-untyped-call ignores cover
        # presidio's untyped constructors, unused-ignore covers the not-installed
        # env (where the imported names are Any).
        nlp_engine: Any = NlpEngineProvider(  # type: ignore[no-untyped-call, unused-ignore]
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": language, "model_name": props.presidio.model}],
            }
        ).create_engine()
        self._analyzer: Any = AnalyzerEngine(nlp_engine=nlp_engine)  # type: ignore[no-untyped-call, unused-ignore]
        self._anonymizer: Any = AnonymizerEngine()  # type: ignore[no-untyped-call, unused-ignore]
        self._language = language
        self._threshold = props.presidio.score_threshold
        # Regex pass runs after presidio to also mask token-types presidio has no
        # recognizer for (JWT, bearer tokens, URL credentials).
        self._fallback = RegexRedactor(props.entities, props.mask, props.extra_patterns)

    def redact(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        result = text
        try:
            # Detect with presidio's full recognizer set (its entity names differ
            # from the regex engine's, so we do NOT restrict to props.entities).
            findings = self._analyzer.analyze(text=result, language=self._language)
            findings = [r for r in findings if r.score >= self._threshold]
            if findings:
                result = self._anonymizer.anonymize(text=result, analyzer_results=findings).text
        except Exception:  # noqa: BLE001 — never let redaction crash logging
            pass
        # Always run the regex pass too, for the token-types presidio misses.
        return self._fallback.redact(result)


def build_redactor(props: RedactionProperties) -> Redactor | None:
    """Resolve the configured redactor, or None when redaction is disabled."""
    if not props.enabled:
        return None
    engine = props.engine.lower().strip()
    if engine in ("presidio", "auto"):
        # Construction fails with ImportError when presidio isn't installed, or
        # e.g. OSError when it's installed but its NLP model isn't downloaded.
        # Either way, fall back to the regex engine — a logging misconfiguration
        # must never crash the application (audit-grade robustness).
        try:
            return PresidioRedactor(props)
        except Exception as exc:  # noqa: BLE001 — any presidio failure -> regex fallback
            if engine == "presidio":
                _logger.warning("presidio PII engine unavailable (%s); falling back to regex redaction", exc)
            else:
                _logger.info("presidio not available (%s); using regex PII redaction (install pyfly[pii])", exc)
    return RegexRedactor(props.entities, props.mask, props.extra_patterns)
