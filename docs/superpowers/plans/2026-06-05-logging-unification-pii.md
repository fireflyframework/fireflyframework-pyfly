# Unified Logging, Spring-style Config & PII Redaction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Intercept and uniformly format every logger (framework + third-party), add Spring-style `pyfly.logging.*` configuration (patterns, file output, rotation, external config file), and redact PII from all log records by default (regex engine; optional Presidio) with an opt-in stdout/stderr redactor.

**Architecture:** New `pyfly.logging.redaction` subpackage (engine + patterns + processor/filter + stream wrapper) and two layout/loader helpers, wired symmetrically into the existing `StructlogAdapter` (structlog `ProcessorFormatter` + `foreign_pre_chain`) and `StdlibLoggingAdapter` (stdlib `Formatter` + `RedactionFilter`). `LoggingProperties` is expanded with nested dataclasses bound via the existing relaxed `@config_properties` binding.

**Tech Stack:** Python 3.12, stdlib `logging` + `logging.config`, `structlog` (optional, default adapter), `presidio-analyzer`/`presidio-anonymizer` (optional `pyfly[pii]` extra), `pytest`, `ruff`, `mypy --strict`.

**Conventions (match the repo):** Apache license header on every file; `from __future__ import annotations`; tests under `tests/<area>/`; run `uv run` tools or `.venv/bin/*`; **CI lint runs tree-wide** — finish each task with `ruff check src/ tests/`, `ruff format src/ tests/`, `mypy src/pyfly`. Commit after each task.

**Spec:** `docs/superpowers/specs/2026-06-05-logging-unification-pii-design.md`

---

## File structure

| File | Responsibility |
|------|----------------|
| `src/pyfly/config/properties/logging.py` (modify) | Expanded `LoggingProperties` + nested `Pattern/File/Rolling/Redaction/Streams/Presidio` dataclasses |
| `src/pyfly/logging/redaction/__init__.py` (create) | Re-exports |
| `src/pyfly/logging/redaction/patterns.py` (create) | Built-in PII regexes + validators (Luhn) + entity registry |
| `src/pyfly/logging/redaction/engine.py` (create) | `Redactor` protocol, `RegexRedactor`, `PresidioRedactor`, `build_redactor()` |
| `src/pyfly/logging/redaction/processor.py` (create) | structlog redaction processor + stdlib `RedactionFilter` |
| `src/pyfly/logging/redaction/stream.py` (create) | `RedactingTextIO` + `install_stream_redaction()` |
| `src/pyfly/logging/layout.py` (create) | `compile_pattern()` logback→Python format mapping |
| `src/pyfly/logging/config_loader.py` (create) | `apply_external_config()` dictConfig/fileConfig |
| `src/pyfly/logging/handlers.py` (create) | `build_file_handler()` rotation helper + `parse_size()` |
| `src/pyfly/logging/stdlib_adapter.py` (modify) | Unified formatter + redaction + file/rotation + external config + streams |
| `src/pyfly/logging/structlog_adapter.py` (modify) | ProcessorFormatter + foreign_pre_chain + redaction processor + file/rotation + external config + streams |
| `pyproject.toml` (modify) | `[project.optional-dependencies] pii` |
| `docs/modules/logging.md` (modify) | Document the new capabilities |

Tests mirror under `tests/logging/`.

---

## Task 1: Expand `LoggingProperties` with Spring-parity nested config

**Files:**
- Modify: `src/pyfly/config/properties/logging.py`
- Test: `tests/logging/test_logging_properties.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_logging_properties.py
# (Apache header omitted here for brevity — include the standard 13-line header.)
from __future__ import annotations

from pyfly.config.properties.logging import LoggingProperties
from pyfly.core.config import Config


def test_defaults():
    props = Config({}).bind(LoggingProperties)
    assert props.level == {"root": "INFO"}
    assert props.format == "console"
    assert props.redaction.enabled is True
    assert props.redaction.engine == "auto"
    assert "EMAIL" in props.redaction.entities
    assert props.redaction.deny_fields == ["password", "token", "secret"]
    assert props.redaction.streams.enabled is False
    assert props.file.name == ""


def test_relaxed_nested_kebab_keys_bind():
    cfg = Config(
        {
            "pyfly": {
                "logging": {
                    "format": "json",
                    "file": {"name": "app.log", "path": "./logs"},
                    "rolling": {"max-size": "10MB", "max-history": 5},
                    "pattern": {"console": "%d %p %c - %m"},
                    "redaction": {
                        "engine": "regex",
                        "mask": "partial",
                        "entities": ["EMAIL"],
                        "extra-patterns": {"EMP": "EMP-\\d+"},
                        "deny-fields": ["pw"],
                        "streams": {"enabled": True},
                        "presidio": {"score-threshold": 0.7, "languages": ["en", "es"]},
                    },
                }
            }
        }
    )
    props = cfg.bind(LoggingProperties)
    assert props.format == "json"
    assert props.file.name == "app.log"
    assert props.rolling.max_size == "10MB"
    assert props.rolling.max_history == 5
    assert props.pattern.console == "%d %p %c - %m"
    assert props.redaction.engine == "regex"
    assert props.redaction.mask == "partial"
    assert props.redaction.entities == ["EMAIL"]
    assert props.redaction.extra_patterns == {"EMP": "EMP-\\d+"}
    assert props.redaction.deny_fields == ["pw"]
    assert props.redaction.streams.enabled is True
    assert props.redaction.presidio.score_threshold == 0.7
    assert props.redaction.presidio.languages == ["en", "es"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_logging_properties.py -q`
Expected: FAIL (`AttributeError: ... has no attribute 'redaction'`).

- [ ] **Step 3: Implement the expanded properties**

Replace the body of `src/pyfly/config/properties/logging.py` (keep the existing Apache header) with:

```python
"""Logging subsystem configuration properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.core.config import config_properties


@dataclass
class PatternProperties:
    """Custom log layout patterns (logback-style tokens)."""

    console: str = ""
    file: str = ""


@dataclass
class FileProperties:
    """File appender — set ``name`` to enable file output (console stays on)."""

    name: str = ""
    path: str = ""


@dataclass
class RollingProperties:
    """Rotation policy for the file appender."""

    max_size: str = "10MB"
    max_history: int = 7
    total_size_cap: str = ""


@dataclass
class StreamsRedactionProperties:
    """Opt-in stdout/stderr redaction wrapper."""

    enabled: bool = False


@dataclass
class PresidioProperties:
    """Microsoft Presidio engine settings (used when engine=presidio|auto)."""

    languages: list[str] = field(default_factory=lambda: ["en"])
    score_threshold: float = 0.5


@dataclass
class RedactionProperties:
    """PII redaction settings (pyfly.logging.redaction.*)."""

    enabled: bool = True
    engine: str = "auto"  # regex | presidio | auto
    entities: list[str] = field(
        default_factory=lambda: [
            "EMAIL",
            "CREDIT_CARD",
            "IBAN",
            "US_SSN",
            "JWT",
            "BEARER_TOKEN",
            "URL_CREDENTIALS",
            "PHONE",
        ]
    )
    mask: str = "placeholder"  # placeholder | partial | hash
    extra_patterns: dict[str, str] = field(default_factory=dict)
    allow_fields: list[str] = field(default_factory=list)
    deny_fields: list[str] = field(default_factory=lambda: ["password", "token", "secret"])
    streams: StreamsRedactionProperties = field(default_factory=StreamsRedactionProperties)
    presidio: PresidioProperties = field(default_factory=PresidioProperties)


@config_properties(prefix="pyfly.logging")
@dataclass
class LoggingProperties:
    """Configuration for the logging subsystem (pyfly.logging.*)."""

    level: dict[str, Any] = field(default_factory=lambda: {"root": "INFO"})
    format: str = "console"  # console | json | logfmt
    pattern: PatternProperties = field(default_factory=PatternProperties)
    file: FileProperties = field(default_factory=FileProperties)
    rolling: RollingProperties = field(default_factory=RollingProperties)
    config: str = ""  # external dictConfig YAML / fileConfig INI path
    redaction: RedactionProperties = field(default_factory=RedactionProperties)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_logging_properties.py -q`
Expected: PASS. If nested binding fails, confirm `Config.bind` recurses into nested dataclasses (it does via `_bind_dataclass`); if a nested field stays a dict, that's a binder bug to fix in `core/config.py` — but the existing binder already supports nested dataclasses, so this should pass.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/config/properties/logging.py tests/logging/test_logging_properties.py
git commit -m "feat(logging): expand LoggingProperties with Spring-parity nested config"
```

---

## Task 2: PII pattern registry (`redaction/patterns.py`)

**Files:**
- Create: `src/pyfly/logging/redaction/__init__.py`
- Create: `src/pyfly/logging/redaction/patterns.py`
- Test: `tests/logging/test_redaction_patterns.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_redaction_patterns.py  (+ Apache header)
from __future__ import annotations

from pyfly.logging.redaction.patterns import BUILTIN_PATTERNS, VALIDATORS, luhn_valid


def test_patterns_present():
    for ent in ("EMAIL", "CREDIT_CARD", "IBAN", "US_SSN", "JWT", "BEARER_TOKEN", "URL_CREDENTIALS", "PHONE", "IPV4"):
        assert ent in BUILTIN_PATTERNS


def test_email_matches():
    assert BUILTIN_PATTERNS["EMAIL"].search("contact jane.doe@acme.io now")


def test_luhn():
    assert luhn_valid("4111111111111111") is True   # valid test Visa
    assert luhn_valid("4111111111111112") is False
    assert VALIDATORS["CREDIT_CARD"]("4111 1111 1111 1111") is True


def test_credit_card_validator_rejects_random_16_digits():
    assert VALIDATORS["CREDIT_CARD"]("1234567890123456") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_patterns.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/pyfly/logging/redaction/__init__.py  (+ Apache header)
"""PII redaction for the logging subsystem."""
```

```python
# src/pyfly/logging/redaction/patterns.py  (+ Apache header)
"""Built-in PII detection patterns + validators."""

from __future__ import annotations

import re
from collections.abc import Callable

_DIGITS = re.compile(r"\D")


def luhn_valid(value: str) -> bool:
    """True when *value*'s digits pass the Luhn checksum (credit cards)."""
    digits = [int(c) for c in _DIGITS.sub("", value)]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# Compiled built-in PII patterns, keyed by entity name.
BUILTIN_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    "BEARER_TOKEN": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
    "URL_CREDENTIALS": re.compile(r"://[^/\s:@]+:([^/\s:@]+)@"),
    "PHONE": re.compile(r"(?<!\d)(?:\+?\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3}[ .-]?\d{4}(?!\d)"),
    "IPV4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"),
    "IPV6": re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"),
}

# Optional per-entity validators (a match is only redacted when the validator passes).
VALIDATORS: dict[str, Callable[[str], bool]] = {
    "CREDIT_CARD": luhn_valid,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_patterns.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/redaction/__init__.py src/pyfly/logging/redaction/patterns.py tests/logging/test_redaction_patterns.py
git commit -m "feat(logging): built-in PII pattern registry + Luhn validator"
```

---

## Task 3: Redactor engine (`redaction/engine.py`)

**Files:**
- Create: `src/pyfly/logging/redaction/engine.py`
- Test: `tests/logging/test_redaction_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_redaction_engine.py  (+ Apache header)
from __future__ import annotations

from pyfly.config.properties.logging import RedactionProperties
from pyfly.logging.redaction.engine import RegexRedactor, build_redactor


def test_placeholder_mask():
    r = RegexRedactor(["EMAIL"], mask="placeholder")
    assert r.redact("ping jane@acme.io ok") == "ping <EMAIL> ok"


def test_partial_mask():
    r = RegexRedactor(["CREDIT_CARD"], mask="partial")
    out = r.redact("card 4111 1111 1111 1111 end")
    assert out.endswith("1111 end") and "*" in out


def test_extra_patterns():
    r = RegexRedactor([], mask="placeholder", extra_patterns={"EMP": r"EMP-\d{4}"})
    assert r.redact("id EMP-1234 x") == "id <EMP> x"


def test_non_string_passthrough():
    r = RegexRedactor(["EMAIL"])
    assert r.redact(123) == 123  # type: ignore[arg-type]


def test_build_redactor_disabled_returns_none():
    props = RedactionProperties(enabled=False)
    assert build_redactor(props) is None


def test_build_redactor_regex_engine():
    props = RedactionProperties(engine="regex")
    r = build_redactor(props)
    assert r is not None
    assert type(r).__name__ == "RegexRedactor"


def test_build_redactor_auto_falls_back_to_regex_without_presidio():
    props = RedactionProperties(engine="auto")
    r = build_redactor(props)
    assert r is not None
    # presidio is not installed in CI -> regex fallback
    assert type(r).__name__ in ("RegexRedactor", "PresidioRedactor")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_engine.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/pyfly/logging/redaction/engine.py  (+ Apache header)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_engine.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/redaction/engine.py tests/logging/test_redaction_engine.py
git commit -m "feat(logging): redactor engine (regex default + optional presidio + build_redactor)"
```

---

## Task 4: structlog processor + stdlib `RedactionFilter` (`redaction/processor.py`)

**Files:**
- Create: `src/pyfly/logging/redaction/processor.py`
- Test: `tests/logging/test_redaction_processor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_redaction_processor.py  (+ Apache header)
from __future__ import annotations

import logging

from pyfly.logging.redaction.engine import RegexRedactor
from pyfly.logging.redaction.processor import RedactionFilter, make_structlog_redactor


def test_structlog_processor_redacts_event_and_fields():
    r = RegexRedactor(["EMAIL"])
    proc = make_structlog_redactor(r, allow_fields=[], deny_fields=["password"])
    out = proc(None, "info", {"event": "login jane@acme.io", "user": "bob@x.io", "password": "hunter2"})
    assert out["event"] == "login <EMAIL>"
    assert out["user"] == "<EMAIL>"
    assert out["password"] == "<REDACTED>"


def test_structlog_allow_fields_limits_scanning():
    r = RegexRedactor(["EMAIL"])
    proc = make_structlog_redactor(r, allow_fields=["event"], deny_fields=[])
    out = proc(None, "info", {"event": "a jane@acme.io", "note": "keep bob@x.io"})
    assert out["event"] == "a <EMAIL>"
    assert out["note"] == "keep bob@x.io"  # not in allow list -> untouched


def test_stdlib_filter_redacts_message():
    r = RegexRedactor(["EMAIL"])
    flt = RedactionFilter(r, allow_fields=[], deny_fields=[])
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "mail %s", ("jane@acme.io",), None)
    assert flt.filter(rec) is True
    assert rec.getMessage() == "mail <EMAIL>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_processor.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/pyfly/logging/redaction/processor.py  (+ Apache header)
"""structlog redaction processor + stdlib RedactionFilter."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pyfly.logging.redaction.engine import Redactor

_REDACTED = "<REDACTED>"


def make_structlog_redactor(
    redactor: Redactor,
    allow_fields: list[str],
    deny_fields: list[str],
) -> Callable[[Any, str, dict[str, Any]], dict[str, Any]]:
    """Build a structlog processor that redacts the event + string fields."""
    allow = set(allow_fields)
    deny = set(deny_fields)

    def processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key, value in list(event_dict.items()):
            if key in deny:
                event_dict[key] = _REDACTED
                continue
            if allow and key not in allow and key != "event":
                continue
            if isinstance(value, str):
                event_dict[key] = redactor.redact(value)
        return event_dict

    return processor


class RedactionFilter(logging.Filter):
    """Stdlib logging filter that redacts the rendered message of every record.

    Attached to handlers so framework AND third-party records are covered.
    """

    def __init__(self, redactor: Redactor, allow_fields: list[str], deny_fields: list[str]) -> None:
        super().__init__()
        self._redactor = redactor
        self._deny = set(deny_fields)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never drop a log line due to formatting
            return True
        redacted = self._redactor.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        # Redact string extras whose key is denied (e.g. extra={"password": ...}).
        for key in self._deny:
            if isinstance(record.__dict__.get(key), str):
                record.__dict__[key] = _REDACTED
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_processor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/redaction/processor.py tests/logging/test_redaction_processor.py
git commit -m "feat(logging): structlog redaction processor + stdlib RedactionFilter"
```

---

## Task 5: stdout/stderr redactor (`redaction/stream.py`)

**Files:**
- Create: `src/pyfly/logging/redaction/stream.py`
- Test: `tests/logging/test_redaction_stream.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_redaction_stream.py  (+ Apache header)
from __future__ import annotations

import io

from pyfly.logging.redaction.engine import RegexRedactor
from pyfly.logging.redaction.stream import RedactingTextIO


def test_redacts_complete_lines():
    buf = io.StringIO()
    stream = RedactingTextIO(buf, RegexRedactor(["EMAIL"]))
    stream.write("hello jane@acme.io\n")
    assert buf.getvalue() == "hello <EMAIL>\n"


def test_buffers_until_newline():
    buf = io.StringIO()
    stream = RedactingTextIO(buf, RegexRedactor(["EMAIL"]))
    stream.write("partial jane@")
    assert buf.getvalue() == ""  # held until newline/flush
    stream.write("acme.io\n")
    assert buf.getvalue() == "partial <EMAIL>\n"


def test_flush_emits_remainder():
    buf = io.StringIO()
    stream = RedactingTextIO(buf, RegexRedactor(["EMAIL"]))
    stream.write("tail jane@acme.io")
    stream.flush()
    assert buf.getvalue() == "tail <EMAIL>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_stream.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/pyfly/logging/redaction/stream.py  (+ Apache header)
"""Opt-in stdout/stderr redaction wrapper."""

from __future__ import annotations

import sys
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
    sys.stdout = RedactingTextIO(original_out, redactor)  # type: ignore[assignment]
    sys.stderr = RedactingTextIO(original_err, redactor)  # type: ignore[assignment]

    def restore() -> None:
        sys.stdout = original_out
        sys.stderr = original_err

    return restore
```

Add the missing import at the top of the file (with the others): `from collections.abc import Callable`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_redaction_stream.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/redaction/stream.py tests/logging/test_redaction_stream.py
git commit -m "feat(logging): opt-in RedactingTextIO stdout/stderr wrapper"
```

---

## Task 6: logback→Python layout mapping (`layout.py`)

**Files:**
- Create: `src/pyfly/logging/layout.py`
- Test: `tests/logging/test_layout.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_layout.py  (+ Apache header)
from __future__ import annotations

from pyfly.logging.layout import compile_pattern


def test_basic_tokens():
    fmt, datefmt = compile_pattern("%d{%H:%M:%S} %p %c - %m")
    assert fmt == "%(asctime)s %(levelname)s %(name)s - %(message)s"
    assert datefmt == "%H:%M:%S"


def test_aliases_and_truncation():
    fmt, datefmt = compile_pattern("%level %logger{10} %msg%n")
    assert fmt == "%(levelname)s %(name)s %(message)s\n"
    assert datefmt is None


def test_unknown_tokens_pass_through():
    fmt, _ = compile_pattern("LIT %m DONE")
    assert fmt == "LIT %(message)s DONE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_layout.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/pyfly/logging/layout.py  (+ Apache header)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_layout.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/layout.py tests/logging/test_layout.py
git commit -m "feat(logging): logback-style pattern -> Python format mapping"
```

---

## Task 7: file-handler builder + size parsing (`handlers.py`)

**Files:**
- Create: `src/pyfly/logging/handlers.py`
- Test: `tests/logging/test_handlers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_handlers.py  (+ Apache header)
from __future__ import annotations

import logging
import pathlib

from pyfly.config.properties.logging import FileProperties, RollingProperties
from pyfly.logging.handlers import build_file_handler, parse_size


def test_parse_size():
    assert parse_size("10MB") == 10 * 1024 * 1024
    assert parse_size("512KB") == 512 * 1024
    assert parse_size("2GB") == 2 * 1024 * 1024 * 1024
    assert parse_size("4096") == 4096
    assert parse_size("") == 0


def test_build_file_handler(tmp_path: pathlib.Path):
    fp = FileProperties(name="app.log", path=str(tmp_path))
    handler = build_file_handler(fp, RollingProperties(max_size="1MB", max_history=3))
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 1024 * 1024
    assert handler.backupCount == 3
    assert pathlib.Path(handler.baseFilename) == (tmp_path / "app.log")
    handler.close()


def test_build_file_handler_none_without_name():
    assert build_file_handler(FileProperties(), RollingProperties()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_handlers.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/pyfly/logging/handlers.py  (+ Apache header)
"""File-appender construction with size-based rotation."""

from __future__ import annotations

import logging
import logging.handlers
import pathlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyfly.config.properties.logging import FileProperties, RollingProperties

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?B?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def parse_size(value: str) -> int:
    """Parse a human size like ``10MB`` / ``512KB`` / ``4096`` to bytes (0 if empty/invalid)."""
    if not value:
        return 0
    match = _SIZE_RE.match(value)
    if not match:
        return 0
    number, unit = match.groups()
    unit = unit.upper()
    if unit and not unit.endswith("B"):
        unit += "B"
    return int(float(number) * _UNITS.get(unit, 1))


def build_file_handler(
    file_props: FileProperties,
    rolling: RollingProperties,
) -> logging.Handler | None:
    """Build a rotating file handler, or None when no file name is configured."""
    if not file_props.name:
        return None
    directory = pathlib.Path(file_props.path) if file_props.path else pathlib.Path()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / file_props.name
    max_bytes = parse_size(rolling.max_size)
    return logging.handlers.RotatingFileHandler(
        filename=str(target),
        maxBytes=max_bytes,
        backupCount=max(0, rolling.max_history),
        encoding="utf-8",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_handlers.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/handlers.py tests/logging/test_handlers.py
git commit -m "feat(logging): rotating file-handler builder + size parsing"
```

---

## Task 8: external config loader (`config_loader.py`)

**Files:**
- Create: `src/pyfly/logging/config_loader.py`
- Test: `tests/logging/test_config_loader.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_config_loader.py  (+ Apache header)
from __future__ import annotations

import logging
import pathlib

from pyfly.logging.config_loader import apply_external_config


def test_apply_dictconfig_yaml(tmp_path: pathlib.Path):
    cfg = tmp_path / "logging.yaml"
    cfg.write_text(
        "version: 1\n"
        "disable_existing_loggers: false\n"
        "handlers:\n"
        "  console:\n"
        "    class: logging.StreamHandler\n"
        "root:\n"
        "  level: WARNING\n"
        "  handlers: [console]\n"
    )
    assert apply_external_config(str(cfg)) is True
    assert logging.getLogger().level == logging.WARNING


def test_apply_missing_returns_false():
    assert apply_external_config("/nonexistent/logging.yaml") is False


def test_apply_empty_path_returns_false():
    assert apply_external_config("") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_config_loader.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# src/pyfly/logging/config_loader.py  (+ Apache header)
"""Load an external logging config file (dictConfig YAML / fileConfig INI)."""

from __future__ import annotations

import logging
import logging.config
import pathlib

_logger = logging.getLogger("pyfly.logging")


def apply_external_config(path: str) -> bool:
    """Apply an external logging config; return True if it was applied.

    ``*.yaml`` / ``*.yml`` / ``*.json`` are loaded as ``logging.config.dictConfig``;
    ``*.ini`` / ``*.conf`` via ``logging.config.fileConfig``. Failures are logged
    and return False (the adapter then falls back to its inline configuration).
    """
    if not path:
        return False
    file = pathlib.Path(path)
    if not file.is_file():
        _logger.warning("logging config file not found: %s", path)
        return False
    try:
        suffix = file.suffix.lower()
        if suffix in (".yaml", ".yml"):
            import yaml  # type: ignore[import-untyped]

            logging.config.dictConfig(yaml.safe_load(file.read_text(encoding="utf-8")) or {})
        elif suffix == ".json":
            import json

            logging.config.dictConfig(json.loads(file.read_text(encoding="utf-8")))
        else:
            logging.config.fileConfig(str(file), disable_existing_loggers=False)
    except Exception as exc:  # noqa: BLE001 — bad config must not crash startup
        _logger.warning("failed to apply logging config %s: %s", path, exc)
        return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_config_loader.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/config_loader.py tests/logging/test_config_loader.py
git commit -m "feat(logging): external dictConfig/fileConfig loader"
```

---

## Task 9: wire the StdlibLoggingAdapter (unified formatter + redaction + file + external config + streams)

**Files:**
- Modify: `src/pyfly/logging/stdlib_adapter.py`
- Test: `tests/logging/test_stdlib_adapter_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_stdlib_adapter_wiring.py  (+ Apache header)
from __future__ import annotations

import logging
import pathlib

from pyfly.core.config import Config
from pyfly.logging.stdlib_adapter import StdlibLoggingAdapter


def _reset_root() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_third_party_logger_is_redacted_and_formatted(capsys):
    _reset_root()
    adapter = StdlibLoggingAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"format": "console"}}}))
    logging.getLogger("some.thirdparty").warning("user jane@acme.io logged in")
    err = capsys.readouterr()
    out = err.out + err.err
    assert "<EMAIL>" in out
    assert "jane@acme.io" not in out


def test_file_output(tmp_path: pathlib.Path):
    _reset_root()
    adapter = StdlibLoggingAdapter()
    adapter.configure(
        Config(
            {
                "pyfly": {
                    "logging": {
                        "file": {"name": "app.log", "path": str(tmp_path)},
                        "redaction": {"enabled": False},
                    }
                }
            }
        )
    )
    logging.getLogger("x").error("boom")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "boom" in (tmp_path / "app.log").read_text()


def test_redaction_disabled_keeps_raw(capsys):
    _reset_root()
    adapter = StdlibLoggingAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"redaction": {"enabled": False}}}}))
    logging.getLogger("x").warning("mail jane@acme.io")
    cap = capsys.readouterr()
    assert "jane@acme.io" in (cap.out + cap.err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_stdlib_adapter_wiring.py -q`
Expected: FAIL (third-party log not redacted — current adapter uses `basicConfig`/`%(message)s` with no redaction).

- [ ] **Step 3: Implement**

Replace `src/pyfly/logging/stdlib_adapter.py` (keep Apache header). Keep the `_StructuredLogger` class unchanged; rewrite `StdlibLoggingAdapter`:

```python
"""StdlibLoggingAdapter — zero-(hard-)dependency LoggingPort with unified formatting + redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from pyfly.config.properties.logging import LoggingProperties
from pyfly.core.config import Config
from pyfly.logging.config_loader import apply_external_config
from pyfly.logging.handlers import build_file_handler
from pyfly.logging.layout import compile_pattern
from pyfly.logging.redaction.engine import build_redactor
from pyfly.logging.redaction.processor import RedactionFilter
from pyfly.logging.redaction.stream import install_stream_redaction


class _StructuredLogger:
    """Wraps stdlib Logger to accept structlog-style calls: logger.info(event, **kwargs)."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _format(self, event: str, kwargs: dict[str, Any]) -> str:
        if kwargs:
            pairs = " ".join(f"{k}={v}" for k, v in kwargs.items())
            return f"{event} | {pairs}"
        return event

    def debug(self, event: str, **kwargs: Any) -> None:
        self._logger.debug(self._format(event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self._logger.info(self._format(event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self._logger.warning(self._format(event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self._logger.error(self._format(event, kwargs))

    def critical(self, event: str, **kwargs: Any) -> None:
        self._logger.critical(self._format(event, kwargs))

    def exception(self, event: str, **kwargs: Any) -> None:
        self._logger.exception(self._format(event, kwargs))


_JSON_FMT = '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
_DEFAULT_CONSOLE = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class StdlibLoggingAdapter:
    """Fallback LoggingPort using only stdlib logging — now with unified formatting + redaction."""

    def __init__(self) -> None:
        self._restore_streams: Callable[[], None] | None = None

    def configure(self, config: Config) -> None:
        props = config.bind(LoggingProperties)
        redactor = build_redactor(props.redaction)

        root = logging.getLogger()
        root_level = getattr(logging, str(props.level.get("root", "INFO")).upper(), logging.INFO)
        root.setLevel(root_level)
        for handler in list(root.handlers):
            root.removeHandler(handler)

        if props.config and apply_external_config(props.config):
            handlers = list(root.handlers)
        else:
            handlers = self._build_handlers(props)
            for handler in handlers:
                root.addHandler(handler)

        if redactor is not None:
            redaction_filter = RedactionFilter(redactor, props.redaction.allow_fields, props.redaction.deny_fields)
            for handler in handlers:
                handler.addFilter(redaction_filter)

        for name, level in props.level.items():
            if name != "root":
                self.set_level(name, str(level))

        if self._restore_streams is not None:
            self._restore_streams()
            self._restore_streams = None
        if redactor is not None and props.redaction.streams.enabled:
            self._restore_streams = install_stream_redaction(redactor)

    def _build_handlers(self, props: LoggingProperties) -> list[logging.Handler]:
        if props.format == "json":
            console_fmt, datefmt = _JSON_FMT, None
        elif props.pattern.console:
            console_fmt, datefmt = compile_pattern(props.pattern.console)
        else:
            console_fmt, datefmt = _DEFAULT_CONSOLE, None

        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(logging.Formatter(console_fmt, datefmt=datefmt))
        handlers: list[logging.Handler] = [console]

        file_handler = build_file_handler(props.file, props.rolling)
        if file_handler is not None:
            if props.pattern.file:
                file_fmt, file_datefmt = compile_pattern(props.pattern.file)
            elif props.format == "json":
                file_fmt, file_datefmt = _JSON_FMT, None
            else:
                file_fmt, file_datefmt = _DEFAULT_CONSOLE, None
            file_handler.setFormatter(logging.Formatter(file_fmt, datefmt=file_datefmt))
            handlers.append(file_handler)
        return handlers

    def get_logger(self, name: str) -> Any:
        return _StructuredLogger(logging.getLogger(name))

    def set_level(self, name: str, level: str) -> None:
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger(name).setLevel(log_level)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_stdlib_adapter_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/stdlib_adapter.py tests/logging/test_stdlib_adapter_wiring.py
git commit -m "feat(logging): stdlib adapter — unified formatting, file output, redaction, external config"
```

---

## Task 10: wire the StructlogAdapter (ProcessorFormatter + foreign_pre_chain + redaction)

**Files:**
- Modify: `src/pyfly/logging/structlog_adapter.py`
- Test: `tests/logging/test_structlog_adapter_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_structlog_adapter_wiring.py  (+ Apache header)
from __future__ import annotations

import logging

import pytest

structlog = pytest.importorskip("structlog")

from pyfly.core.config import Config  # noqa: E402
from pyfly.logging.structlog_adapter import StructlogAdapter  # noqa: E402


def _reset_root() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_foreign_logger_formatted_and_redacted(capsys):
    _reset_root()
    adapter = StructlogAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"format": "console"}}}))
    # A plain stdlib (foreign) logger — must be rendered through the unified
    # formatter AND redacted.
    logging.getLogger("sqlalchemy.engine").warning("connect user=jane@acme.io")
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "<EMAIL>" in out
    assert "jane@acme.io" not in out
    assert "warning" in out.lower()  # level rendered (unified format), not bare message


def test_structlog_event_redacted(capsys):
    _reset_root()
    adapter = StructlogAdapter()
    adapter.configure(Config({"pyfly": {"logging": {"format": "console"}}}))
    adapter.get_logger("app").info("login", email="jane@acme.io")
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "<EMAIL>" in out
    assert "jane@acme.io" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_structlog_adapter_wiring.py -q`
Expected: FAIL (foreign logger not redacted; current adapter uses `basicConfig`).

- [ ] **Step 3: Implement**

Replace `src/pyfly/logging/structlog_adapter.py` (keep Apache header):

```python
"""StructlogAdapter — default LoggingPort with unified formatting + PII redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

import structlog

from pyfly.config.properties.logging import LoggingProperties
from pyfly.core.config import Config
from pyfly.logging.config_loader import apply_external_config
from pyfly.logging.handlers import build_file_handler
from pyfly.logging.redaction.engine import build_redactor
from pyfly.logging.redaction.processor import make_structlog_redactor
from pyfly.logging.redaction.stream import install_stream_redaction


class StructlogAdapter:
    """Default logging adapter backed by structlog with one formatter for all records."""

    def __init__(self) -> None:
        self._restore_streams: Callable[[], None] | None = None

    def configure(self, config: Config) -> None:
        props = config.bind(LoggingProperties)
        redactor = build_redactor(props.redaction)
        fmt = props.format.lower()

        # Shared pre-chain applied to BOTH structlog and foreign (stdlib) records.
        timestamper = structlog.processors.TimeStamper(fmt="iso")
        shared_pre: list[structlog.types.Processor] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
        ]
        if redactor is not None:
            shared_pre.append(
                make_structlog_redactor(redactor, props.redaction.allow_fields, props.redaction.deny_fields)
            )

        renderer: structlog.types.Processor = (
            structlog.processors.JSONRenderer()
            if fmt == "json"
            else structlog.processors.KeyValueRenderer(key_order=["timestamp", "level", "logger", "event"])
            if fmt == "logfmt"
            else structlog.dev.ConsoleRenderer(colors=False, sort_keys=False)
        )

        # structlog -> stdlib bridge: ProcessorFormatter renders the final record.
        structlog.configure(
            processors=[*shared_pre, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_pre,
        )

        root = logging.getLogger()
        root.setLevel(getattr(logging, str(props.level.get("root", "INFO")).upper(), logging.INFO))
        for handler in list(root.handlers):
            root.removeHandler(handler)

        if props.config and apply_external_config(props.config):
            handlers = list(root.handlers)
            for handler in handlers:
                handler.setFormatter(formatter)
        else:
            console = logging.StreamHandler(stream=sys.stdout)
            console.setFormatter(formatter)
            handlers = [console]
            file_handler = build_file_handler(props.file, props.rolling)
            if file_handler is not None:
                file_handler.setFormatter(formatter)
                handlers.append(file_handler)
            for handler in handlers:
                root.addHandler(handler)

        # NOTE: redaction is handled entirely by ``make_structlog_redactor`` in
        # ``shared_pre`` — which runs for structlog records (it's in the configured
        # processor chain) AND for foreign stdlib records (it's the
        # ``foreign_pre_chain``). We deliberately do NOT add a handler-level
        # RedactionFilter here: at filter time a structlog record's ``msg`` is the
        # wrapped event dict, so redacting its ``getMessage()`` would corrupt the
        # value ProcessorFormatter expects.

        for name, level in props.level.items():
            if name != "root":
                self.set_level(name, str(level))

        if self._restore_streams is not None:
            self._restore_streams()
            self._restore_streams = None
        if redactor is not None and props.redaction.streams.enabled:
            self._restore_streams = install_stream_redaction(redactor)

    def get_logger(self, name: str) -> Any:
        return structlog.get_logger(name)

    def set_level(self, name: str, level: str) -> None:
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger(name).setLevel(log_level)
```

> Note: the structured-field redactor lives in `shared_pre`, so it runs for both
> structlog records (it's in the configured processor chain) and foreign stdlib
> records (it's the `foreign_pre_chain`). Unlike the stdlib adapter, the structlog
> adapter does **not** attach a handler-level `RedactionFilter` — a structlog
> record's `msg` is a wrapped event dict at filter time, so redacting its
> `getMessage()` would corrupt what `ProcessorFormatter` expects.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_structlog_adapter_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pyfly/logging/structlog_adapter.py tests/logging/test_structlog_adapter_wiring.py
git commit -m "feat(logging): structlog adapter — unified ProcessorFormatter + redaction"
```

---

## Task 11: declare the `pyfly[pii]` optional extra

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/logging/test_pii_extra_declared.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logging/test_pii_extra_declared.py  (+ Apache header)
from __future__ import annotations

import pathlib
import tomllib


def test_pii_extra_declared():
    data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "pii" in extras
    joined = " ".join(extras["pii"]).lower()
    assert "presidio-analyzer" in joined
    assert "presidio-anonymizer" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/logging/test_pii_extra_declared.py -q`
Expected: FAIL (`KeyError: 'pii'`).

- [ ] **Step 3: Implement**

In `pyproject.toml`, under `[project.optional-dependencies]`, add (match the existing
formatting of sibling extras):

```toml
pii = [
    "presidio-analyzer>=2.2",
    "presidio-anonymizer>=2.2",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/logging/test_pii_extra_declared.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/logging/test_pii_extra_declared.py
git commit -m "feat(logging): add pyfly[pii] optional extra (presidio)"
```

---

## Task 12: documentation + full regression

**Files:**
- Modify: `docs/modules/logging.md`
- Test: full suite + lint + typecheck

- [ ] **Step 1: Update the logging guide**

Edit `docs/modules/logging.md` to add sections covering: (a) **Unified interception** — that all loggers (framework + third-party) now render through one formatter; (b) **Configuration** — the full `pyfly.logging.*` key table from the spec (level, format, pattern.console/file, file.name/path, rolling.*, config external file) with a YAML example; (c) **PII redaction** — `redaction.*` keys, regex default + `pyfly[pii]`/Presidio upgrade (`engine: auto|regex|presidio`), `mask` styles, `deny-fields`/`allow-fields`, and the opt-in `redaction.streams.enabled` stdout/stderr wrapper. Base every key/example on the implemented code. Add the doc to the module index if not already present (it is).

- [ ] **Step 2: Run the targeted logging suite**

Run: `.venv/bin/python -m pytest tests/logging/ -q`
Expected: PASS (all new tests).

- [ ] **Step 3: Run lint + typecheck (CI parity — tree-wide)**

Run:
```bash
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format src/ tests/
.venv/bin/mypy src/pyfly
```
Expected: all clean. Fix any findings (e.g. unused imports, `type: ignore` placement) and re-run.

- [ ] **Step 4: Run the full suite (catch any log-format assertions that changed)**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: PASS. If any pre-existing test asserts a specific third-party/console log string, update it to reflect the unified format (the intended change) — verify each is a formatting-only difference, not a regression.

- [ ] **Step 5: Commit**

```bash
git add docs/modules/logging.md
git commit -m "docs(logging): document unified formatting, Spring-style config, and PII redaction"
```

---

## Self-review notes (addressed)

- **Spec coverage:** unified formatting (Tasks 9–10) ✓; Spring keys/patterns/file/rolling (Tasks 1, 6, 7, 9, 10) ✓; external config (Task 8) ✓; regex redactor default + Presidio opt-in (Tasks 2, 3, 11) ✓; processor/filter (Task 4) ✓; opt-in stream wrapper (Task 5) ✓; both adapters symmetric (Tasks 9–10) ✓; testing plan (every task is TDD) ✓; docs (Task 12) ✓.
- **Type consistency:** `build_redactor(props: RedactionProperties) -> Redactor | None`, `make_structlog_redactor(redactor, allow_fields, deny_fields)`, `RedactionFilter(redactor, allow_fields, deny_fields)`, `compile_pattern(spec) -> (str, str|None)`, `build_file_handler(file_props, rolling) -> Handler | None`, `apply_external_config(path) -> bool`, `install_stream_redaction(redactor) -> Callable[[],None]` — names/signatures consistent across tasks.
- **Known nuance:** redaction is split by adapter. The **stdlib** adapter has no processor chain, so it redacts at the handler via `RedactionFilter` on the rendered message. The **structlog** adapter redacts in `shared_pre` (used as both the processor chain and the `foreign_pre_chain`) and deliberately omits a handler filter, because a structlog record's `msg` is a wrapped event dict at filter time. Verified consistent in Tasks 9 vs 10.
