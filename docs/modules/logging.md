# Logging Guide

PyFly uses **structured logging** — every log call is an event name plus
key/value fields, rendered either by [`structlog`](https://www.structlog.org/)
(when installed) or a zero-dependency stdlib fallback.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Getting a logger](#getting-a-logger)
3. [The LoggingPort](#the-loggingport)
4. [Adapters](#adapters)
5. [Unified interception](#unified-interception)
6. [Configuration](#configuration)
7. [PII redaction](#pii-redaction)

---

## Introduction

The logging module is a hexagonal port (`LoggingPort`) with two adapters:

- **`StructlogAdapter`** — used when the `observability` extra (which ships
  `structlog`) is installed. Produces rich, processor-based structured output.
- **`StdlibLoggingAdapter`** — a zero-dependency fallback built on the standard
  library `logging` module. Renders structured fields as `event | key=value`.

Both accept the same **structlog-style call signature**: an event string
followed by arbitrary keyword fields.

```python
logger.info("http_request", method="GET", path="/orders", status_code=200)
```

---

## Getting a logger

Use `get_logger` — it returns a structured logger backed by `structlog` when
available, and the stdlib shim otherwise, so your call sites are identical
regardless of which extras are installed:

```python
from pyfly.logging import get_logger

logger = get_logger(__name__)

logger.info("order_placed", order_id="o-123", total=49.90)
logger.warning("retrying", attempt=2)
logger.error("payment_failed", error="declined", order_id="o-123")
```

> **Why not the stdlib logger directly?** A raw `logging.Logger` rejects
> arbitrary keyword arguments (`logger.info("event", method="GET")` raises
> `TypeError`). `get_logger` guarantees the structured signature works even
> without `structlog` installed.

---

## The LoggingPort

```python
from typing import Any, Protocol, runtime_checkable
from pyfly.core.config import Config

@runtime_checkable
class LoggingPort(Protocol):
    def configure(self, config: Config) -> None: ...
    def get_logger(self, name: str) -> Any: ...
    def set_level(self, name: str, level: str) -> None: ...
```

The active adapter is selected automatically at startup and configured from the
`pyfly.logging.*` section.

---

## Adapters

| Adapter | When | Output |
|---|---|---|
| `StructlogAdapter` | `structlog` installed | structured (console or JSON) via structlog processors |
| `StdlibLoggingAdapter` | fallback | `event | key=value` via stdlib `logging` |

The stdlib shim (`_StructuredLogger`) wraps a `logging.Logger` and accepts
`debug/info/warning/error/critical/exception(event, **kwargs)`.

---

## Unified interception

All loggers — framework, application, and **third-party** (e.g. `sqlalchemy`,
`httpx`, `uvicorn`) — render through **one** formatter and one redaction pass.

**StructlogAdapter** installs a `ProcessorFormatter` on the root handler and
passes the same processor list as `foreign_pre_chain`, so every stdlib record
(even those emitted by libraries that know nothing about structlog) goes through
the timestamp, level, logger-name, and PII redaction steps before hitting the
output stream.

**StdlibLoggingAdapter** attaches a `RedactionFilter` to every handler on the
root logger. Because all loggers propagate to root by default, third-party
records are intercepted automatically.

The result is that you can safely emit a raw `logging.getLogger("sqlalchemy").warning(...)`
from any dependency and it will be formatted and redacted identically to your
own structured log calls — without any per-library configuration.

---

## Configuration

Full key reference for `pyfly.logging.*` (values shown are defaults):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `level.root` | string | `INFO` | Root log level |
| `level.<name>` | string | — | Per-logger level override (e.g. `"pyfly.web": DEBUG`) |
| `format` | string | `console` | Output renderer: `console`, `json`, or `logfmt` |
| `pattern.console` | string | `""` | Logback-style pattern for the console handler (see below) |
| `pattern.file` | string | `""` | Logback-style pattern for the file handler |
| `file.name` | string | `""` | File appender filename — set to enable file output |
| `file.path` | string | `""` | Directory for the file appender (created if absent) |
| `rolling.max-size` | string | `10MB` | Max size per file before rotation (`KB`/`MB`/`GB`) |
| `rolling.max-history` | integer | `7` | Number of rotated files to keep |
| `rolling.total-size-cap` | string | `""` | Reserved for forward-compatibility; **accepted but not yet enforced** (Python's `RotatingFileHandler` has no total-size cap — use `max-history` to bound retention) |
| `config` | string | `""` | Path to an external `dictConfig` YAML/JSON or `fileConfig` INI |

Example YAML:

```yaml
pyfly:
  logging:
    level:
      root: INFO
      "pyfly.web": DEBUG           # per-logger overrides
      "sqlalchemy.engine": WARNING
    format: console                # console | json | logfmt
    pattern:
      console: "%d{%H:%M:%S} %p %c - %m"   # logback-style tokens
    file:
      name: app.log
      path: ./logs
    rolling:
      max-size: 50MB
      max-history: 14
```

### Logback-style pattern tokens

`pattern.console` and `pattern.file` accept a subset of logback tokens:

| Token | Python equivalent | Notes |
|-------|------------------|-------|
| `%d{fmt}` | `%(asctime)s` | `fmt` passed as `datefmt`; bare `%d` uses default |
| `%p` / `%level` | `%(levelname)s` | |
| `%c` / `%logger{N}` | `%(name)s` | `{N}` truncation accepted but ignored |
| `%m` / `%msg` / `%message` | `%(message)s` | |
| `%t` / `%thread` | `%(threadName)s` | |
| `%n` | newline | |

### External config file

Set `pyfly.logging.config` to a path to delegate entirely to an external file:

- **YAML/JSON** — loaded via `logging.config.dictConfig` (requires `version: 1`)
- **INI** — loaded via `logging.config.fileConfig` (standard `[loggers]`/`[handlers]`/`[formatters]` sections)

If the file is missing or fails to parse, the adapter logs a warning and falls
back to its inline configuration. PyFly attaches the PII redaction filter to
whatever handlers the external config installs. If the external config installs
**no** handlers on the root logger, PyFly does not add any — this is intentional
"you took full control" behavior (per-logger `level.*` overrides are still
applied, but redaction only covers handlers that exist).

The admin dashboard's **Loggers** view and the `/actuator/loggers` endpoint let
you inspect and change levels at runtime.

---

## PII redaction

PII redaction is **on by default** and scans every log record's rendered
message for sensitive entities before writing to any output.

### Configuration keys (`pyfly.logging.redaction.*`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `redaction.enabled` | bool | `true` | Master switch — set to `false` to disable entirely |
| `redaction.engine` | string | `auto` | `regex` (always available), `presidio` (requires `pyfly[pii]`), or `auto` (uses Presidio if installed, falls back to regex) |
| `redaction.entities` | list[string] | see below | Entity names to detect and mask |
| `redaction.mask` | string | `placeholder` | Mask style: `placeholder` (`<EMAIL>`), `partial` (`****1111`), or `hash` (`<EMAIL:a1b2c3d4>`) |
| `redaction.extra-patterns` | map[string, string] | `{}` | Additional named regex patterns to redact (e.g. `{"EMP": "EMP-\\d{4}"}`) |
| `redaction.deny-fields` | list[string] | `["password", "token", "secret"]` | Structured log fields whose value is always replaced with `<REDACTED>` |
| `redaction.allow-fields` | list[string] | `[]` | When set, only these structured fields (plus `event`) are scanned; others are skipped |
| `redaction.streams.enabled` | bool | `false` | Opt-in: wrap `sys.stdout`/`sys.stderr` with the redactor |
| `redaction.presidio.languages` | list[string] | `["en"]` | Languages passed to Presidio's `AnalyzerEngine` |
| `redaction.presidio.score-threshold` | float | `0.5` | Minimum Presidio confidence score to trigger redaction |
| `redaction.presidio.model` | string | `en_core_web_lg` | spaCy model Presidio's NLP engine loads (download it with `python -m spacy download <model>`; use a smaller model like `en_core_web_sm` for lighter footprints) |

Default entities detected by the regex engine:

`EMAIL`, `CREDIT_CARD` (Luhn-validated), `IBAN`, `US_SSN`, `JWT`,
`BEARER_TOKEN`, `URL_CREDENTIALS`, `PHONE`. (`IPV4` and `IPV6` patterns also
ship but are **off by default** — IP addresses are common in logs and redacting
them is often undesirable; enable them via `redaction.entities` if you need them.)

### Upgrading to Presidio (NER-based redaction)

Install the optional extra:

```bash
pip install "pyfly[pii]"
# or
uv add "pyfly[pii]"
```

You also need a spaCy model (Presidio defaults to `en_core_web_lg`):

```bash
python -m spacy download en_core_web_lg   # or en_core_web_sm for a lighter footprint
```

Then set `engine: auto` (default) or `engine: presidio`. Presidio detects with its
full recognizer set (named-entity recognition), catching PII that regex cannot —
free-text **names**, locations, etc. — and pyfly then runs the regex pass over the
result so token-types Presidio has no recognizer for (JWT, bearer tokens, URL
credentials) are still masked. If the model isn't installed, redaction falls back
to the regex engine rather than failing.

```yaml
pyfly:
  logging:
    redaction:
      engine: auto          # uses Presidio when installed, regex otherwise
      mask: partial
      presidio:
        languages: [en, es]
        score-threshold: 0.6
```

### Opt-in stdout/stderr wrapper

Set `redaction.streams.enabled: true` to wrap `sys.stdout` and `sys.stderr` with
a line-buffered redactor. This catches any raw `print()` calls or third-party
code that bypasses the logging system entirely. Partial lines are held in a
buffer until a newline is written (or `flush()` is called), so multi-write PII
spanning two `write()` calls is still caught.

```yaml
pyfly:
  logging:
    redaction:
      streams:
        enabled: true
```

> This wrapper is opt-in because it replaces the process-wide `sys.stdout` /
> `sys.stderr` references, which can interfere with interactive tools (debuggers,
> Jupyter, etc.). Leave it off in development unless you specifically need it.
