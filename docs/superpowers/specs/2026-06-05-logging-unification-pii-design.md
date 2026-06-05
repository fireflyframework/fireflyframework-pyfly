# Design: Unified Logging, Spring-style Configuration & PII Redaction

**Date:** 2026-06-05
**Status:** Approved (brainstorming) — pending implementation plan
**Module:** `pyfly.logging`

---

## Goal

Make pyfly's logging subsystem "perfectly handled":

1. **Intercept and uniformly format every log** — framework, third-party libraries
   (uvicorn, sqlalchemy, kafka, httpx, …), and anything routed through stdlib
   `logging` — so all output shares one timestamp/level/structure format.
2. **Spring-style configuration** — rich `pyfly.logging.*` keys (levels, patterns,
   file output, rotation) for the common cases, plus an external config-file escape
   hatch for full control.
3. **PII redaction** — mask personally identifiable information in every log record
   by default (fast regex engine), with an optional Microsoft Presidio upgrade, and
   an opt-in stdout/stderr interceptor for raw `print()` / direct writes.

These build on the existing hexagonal `LoggingPort` and its two adapters; nothing
in the current public API is removed.

---

## Current state (baseline)

`pyfly.logging` is a `LoggingPort` Protocol (`configure(config)` / `get_logger(name)`
/ `set_level(name, level)`) with two adapters:

- **`StructlogAdapter`** (default; needs `structlog` from the `observability` extra):
  processor chain → contextvars, logger name, level, ISO timestamp, stack info →
  JSON or console renderer. Routes through stdlib via
  `logging.basicConfig(format="%(message)s", stream=sys.stdout, force=True)`.
- **`StdlibLoggingAdapter`** (zero-dependency fallback): renders `event | key=value`.

Config today is only `pyfly.logging.level.<logger>` (root + per-logger) and
`pyfly.logging.format` (`console` | `json`). Runtime level changes are exposed via
`/actuator/loggers` and the admin Loggers view; `AdminLogHandler` tees records to
the dashboard.

### Gaps this design closes

- **Non-uniform formatting.** Third-party stdlib loggers reach the root handler but
  render with bare `%(message)s`, bypassing the structlog formatting (no timestamp,
  level, or structure). There is no true "intercept everything" path.
- **No file/rotation/pattern config.** No file appender, rotation, custom layout, or
  external config file.
- **No PII redaction** anywhere.

---

## Decisions (from brainstorming)

| Decision | Choice |
|----------|--------|
| PII engine packaging | Fast built-in **regex redactor ON by default**; `pyfly[pii]` extra **auto-upgrades to Presidio** (`engine=auto`). |
| Interception scope | **Log records always redacted**; **stdout/stderr wrapper opt-in** (default off), framework rich console bypassed. |
| Config model | **Spring-parity `pyfly.logging.*` keys + external config-file escape hatch** (`pyfly.logging.config`). |
| Formatting approach | **structlog-centric**: `ProcessorFormatter` + `foreign_pre_chain` formats all records; implemented symmetrically in the stdlib fallback. |

---

## Architecture

All new code lives under `src/pyfly/logging/`. Units are small and independently
testable.

```
pyfly/logging/
  port.py                 # LoggingPort (unchanged contract)
  __init__.py             # get_logger() entry (unchanged)
  structlog_adapter.py    # EXTENDED: ProcessorFormatter, file/rotation, redaction wiring, external config
  stdlib_adapter.py       # EXTENDED: matching Formatter + Filter, file/rotation, redaction wiring, external config
  patterns.py             # NEW: logback-style %d/%p/%c/%m pattern -> Python logging format mapping
  config_loader.py        # NEW: load external dictConfig (YAML) / fileConfig (INI) when pyfly.logging.config set
  redaction/
    __init__.py
    engine.py             # NEW: Redactor protocol + RegexRedactor (default) + PresidioRedactor (optional) + build_redactor()
    patterns.py           # NEW: built-in PII regex patterns + entity registry
    processor.py          # NEW: structlog redaction processor + stdlib logging.Filter (RedactionFilter)
    stream.py             # NEW: RedactingTextIO wrapper for sys.stdout/sys.stderr (opt-in)
config/properties/logging.py  # EXTENDED: LoggingProperties with the Spring-parity keys
```

### Component responsibilities

- **`patterns.py` (layout)** — `compile_pattern(spec) -> python_format_str`. Maps the
  common logback conversion words (`%d{...}`→`%(asctime)s` with datefmt, `%p`/`%level`
  →`%(levelname)s`, `%c`/`%logger`→`%(name)s`, `%m`/`%msg`→`%(message)s`,
  `%t`/`%thread`, `%X{key}`→MDC/contextvar) to a Python `logging` format string.
  Unknown tokens pass through literally. Pure function, no I/O.

- **`config_loader.py`** — `apply_external_config(path) -> bool`. If
  `pyfly.logging.config` points at a `*.yaml`/`*.yml` it is loaded as a
  `logging.config.dictConfig`; a `*.ini`/`*.conf` is applied via
  `logging.config.fileConfig`. When set, it takes over handler/formatter setup
  (the adapter still wires the redaction filter onto the resulting handlers).

- **`redaction/engine.py`**
  - `Redactor` Protocol: `redact(text: str) -> str`.
  - `RegexRedactor(entities, mask, extra_patterns)` — compiled built-in patterns;
    fast; default engine. `mask` ∈ `placeholder` (`<EMAIL>`), `partial` (keep last 4),
    `hash` (sha256 short). No new dependencies.
  - `PresidioRedactor(languages, score_threshold, entities, mask)` — lazy-imports
    `presidio_analyzer` + `presidio_anonymizer`; raises a clear error only if used
    while uninstalled.
  - `build_redactor(props) -> Redactor | None` — returns `None` when disabled;
    resolves `engine`: `regex` → RegexRedactor; `presidio` → PresidioRedactor;
    `auto` → Presidio if importable else RegexRedactor (one-time `pyfly.logging`
    warning).

- **`redaction/patterns.py`** — registry of `{entity: compiled_regex}` for EMAIL,
  PHONE, CREDIT_CARD (Luhn-validated to cut false positives), IBAN, US_SSN, IPV4,
  IPV6, JWT, BEARER_TOKEN/API_KEY, URL_CREDENTIALS (userinfo password). Default
  entity set is the low-false-positive subset; configurable via `redaction.entities`.

- **`redaction/processor.py`**
  - `make_structlog_redactor(redactor, allow_fields, deny_fields)` → a structlog
    processor that redacts the `event` and every string-valued field (honoring
    allow/deny field lists), inserted late in the chain (before the renderer).
  - `RedactionFilter(redactor, allow_fields, deny_fields)` → a stdlib
    `logging.Filter` that redacts `record.msg`, string `record.args`, and string
    `record.__dict__` extras. Attached to every handler so third-party records are
    covered. Idempotent / safe on non-string payloads.

- **`redaction/stream.py`** — `RedactingTextIO(wrapped, redactor)` line-buffered
  text wrapper; `install_stream_redaction(redactor)` swaps `sys.stdout`/`sys.stderr`
  and returns a restore handle. **Not** applied to the framework's rich console
  (the CLI builds its `rich.Console` on the original streams, or we pass
  `console=Console(file=<original>)`), so banners/markup are never corrupted. Opt-in
  via `redaction.streams.enabled`.

### Adapter wiring (both adapters, symmetric)

`configure(config)` now:

1. Bind `LoggingProperties`.
2. Build the redactor (`build_redactor`) once.
3. If `pyfly.logging.config` set → `config_loader.apply_external_config()`, then
   attach the redaction filter to all resulting handlers and return.
4. Else build handlers:
   - **Console handler** on stdout with the unified formatter:
     - structlog: `structlog.stdlib.ProcessorFormatter(processor=<renderer>,
       foreign_pre_chain=<shared pre-chain>)`, and the redaction processor added to
       both the structlog chain and the `foreign_pre_chain` so *all* records render
       identically and redacted.
     - stdlib fallback: a `logging.Formatter` from `pattern.console`/`format`, plus a
       `RedactionFilter` on the handler.
   - **File handler** when `file.name`/`file.path` set: `RotatingFileHandler`
     (size policy) or `TimedRotatingFileHandler`, same formatter, same redaction
     filter, honoring `pattern.file`.
5. Install handlers on the root logger (`force`-style: clear existing, add ours),
   apply `level.*` per-logger levels (existing behavior).
6. If `redaction.streams.enabled` → `install_stream_redaction()`.

`set_level` / `get_logger` unchanged.

---

## Configuration keys (Spring parity, under `pyfly.logging`)

All optional; existing keys keep working.

```yaml
pyfly:
  logging:
    level:                       # existing — root + per-logger
      root: INFO
      sqlalchemy.engine: WARNING
    format: console              # console | json | logfmt
    pattern:
      console: "%d{HH:mm:ss} %p %c{1} - %m"   # logback-style tokens
      file: "%d %p [%t] %c - %m"
    file:
      name: app.log              # enables file output (console stays on)
      path: ./logs               # directory; joined with name
    rolling:
      max-size: 10MB             # size-based rotation
      max-history: 7             # backups kept
      total-size-cap: 100MB
    config: ./logging.yaml       # external dictConfig/fileConfig escape hatch
    redaction:
      enabled: true              # default true
      engine: auto               # regex | presidio | auto
      entities: [EMAIL, CREDIT_CARD, IBAN, US_SSN, JWT, BEARER_TOKEN, URL_CREDENTIALS, PHONE]
      mask: placeholder          # placeholder | partial | hash
      extra-patterns:            # name -> regex
        EMPLOYEE_ID: "EMP-\\d{6}"
      allow-fields: []           # only redact these structured fields (when set)
      deny-fields: [password, token, secret]   # always redact these fields whole
      streams:
        enabled: false           # wrap sys.stdout/stderr for raw writes (opt-in)
      presidio:
        languages: [en]
        score-threshold: 0.5
```

`LoggingProperties` (dataclass) gains nested dataclasses for `pattern`, `file`,
`rolling`, and `redaction` (with `streams` and `presidio` sub-objects), bound via
the existing relaxed `@config_properties` binding.

**Precedence rules:**
- `format: json` ignores `pattern.*` (JSON is structured, not a text layout);
  `pattern.*` applies only to `console`/`logfmt` text formats. When `format` is a
  text format and `pattern.console`/`pattern.file` is set, the pattern wins for that
  sink; otherwise a sensible built-in default layout is used.
- `pyfly.logging.config` (external file), when set, supersedes the inline
  handler/formatter keys (`format`, `pattern`, `file`, `rolling`) — the adapter only
  layers the redaction filter and per-logger `level.*` on top of the loaded config.
- The **default redaction entity set** is exactly the list shown in the config
  example below (`EMAIL, CREDIT_CARD, IBAN, US_SSN, JWT, BEARER_TOKEN,
  URL_CREDENTIALS, PHONE`) — the low-false-positive subset; `redaction.entities`
  overrides it.

---

## Dependencies

- New optional extra `pyfly[pii]` → `presidio-analyzer`, `presidio-anonymizer`
  (spaCy model downloaded per Presidio docs). Declared in `pyproject.toml`
  `[project.optional-dependencies]`.
- No new **required** dependencies. Regex redaction is stdlib-only. External YAML
  config uses the already-present `PyYAML`.

---

## Backward compatibility & migration

- `pyfly.logging.level.*` and `pyfly.logging.format` behave exactly as before.
- The console output **format may change** for third-party loggers (they now get the
  unified format instead of bare `%(message)s`) — this is the intended fix. Any
  existing test asserting raw third-party log text must be reviewed and updated.
- Redaction is **on by default** but uses the low-false-positive entity set and
  masks only clear matches; `redaction.enabled: false` fully restores prior output.
- `/actuator/loggers`, admin Loggers view, and `AdminLogHandler` keep working; the
  admin handler shares the `RedactionFilter` so dashboard logs are redacted too.

---

## Performance

- Redaction runs only on string values, with compiled patterns; a single combined
  scan per record. No-op (early return) when `redaction.enabled: false`.
- `allow-fields` limits scanning to named fields for hot paths.
- Presidio (NER) is materially slower; that's why it's opt-in and `auto` prefers
  regex unless explicitly chosen / installed.
- Stream redaction (opt-in) is line-buffered and only added when enabled.

---

## Testing plan

- **engine**: RegexRedactor masks each entity (placeholder/partial/hash); Luhn cuts
  card false positives; `extra-patterns`; disabled → identity. `build_redactor`
  engine resolution (regex/presidio/auto with + without presidio importable).
- **patterns (layout)**: logback tokens → Python format mapping; unknown tokens pass
  through.
- **processor/filter**: structlog processor redacts event + string fields, honors
  allow/deny; `RedactionFilter` redacts `record.msg`/args/extras and is safe on
  non-strings.
- **stream**: `RedactingTextIO` masks written text, line-buffered, restore works,
  rich console on original stream is untouched.
- **config**: Spring keys bind into `LoggingProperties`; external dictConfig YAML +
  fileConfig INI applied; pattern/file/rolling produce the expected handlers.
- **unified formatting (integration)**: a foreign `logging.getLogger("sqlalchemy")`
  line renders with the framework timestamp/level format; end-to-end a third-party
  line containing an email is both formatted and redacted.
- **adapters**: both `StructlogAdapter` and `StdlibLoggingAdapter` pass the same
  behavioral suite (parametrized) so the capability holds with/without structlog.
- **regression**: full suite green; `ruff check src/ tests/` + `ruff format --check`
  + `mypy src/pyfly --strict` clean (CI parity — run tree-wide, not per-file).

---

## Out of scope (explicitly deferred)

- Remote/network log shipping (OTLP logs, Loki, syslog appenders) — future.
- Async/queue log handlers — future; current handlers are synchronous like today.
- Per-request log sampling / rate limiting — future.
- The `pyfly.utils` document-rendering port (separate task, deferred per user).
