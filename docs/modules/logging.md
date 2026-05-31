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
5. [Configuration](#configuration)

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

## Configuration

```yaml
pyfly:
  logging:
    level:
      root: INFO
      "pyfly.web": DEBUG     # per-logger overrides
    format: console          # or "json"
```

- `level.root` — root log level.
- `level.<logger>` — per-logger level overrides.
- `format` — `console` (human-readable) or `json` (one JSON object per line).

The admin dashboard's **Loggers** view and the `/actuator/loggers` endpoint let
you inspect and change levels at runtime.
