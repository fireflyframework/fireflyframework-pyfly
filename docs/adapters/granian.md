# Granian Adapter

> **Module:** Server -- [Module Guide](../modules/server.md)
> **Package:** `pyfly.server.adapters.granian`
> **Backend:** Granian 1.6+, Rust/tokio

## Quick Start

### Installation

```bash
uv add "pyfly[granian]"
```

Or install the full high-performance web stack (Starlette + Granian + uvloop):

```bash
uv add "pyfly[web-fast]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  web:
    port: 8080
    host: "0.0.0.0"
```

No server configuration is needed. Granian is auto-selected as the default server when installed because it has the highest priority in the cascading auto-configuration chain.

### Minimal Example

```python
from pyfly.container import rest_controller
from pyfly.web import request_mapping, get_mapping

@rest_controller
@request_mapping("/api/hello")
class HelloController:
    @get_mapping("")
    async def hello(self) -> dict:
        return {"message": "Hello, World!"}
```

```bash
pyfly run
# Granian auto-selected, serving at http://localhost:8080/api/hello
```

---

## What is Granian?

Granian is a Rust-powered ASGI/RSGI server built on the tokio async runtime. It is the highest-throughput Python ASGI server available, achieving approximately 3x the requests per second of Uvicorn in standard benchmarks.

Key characteristics:

- **Rust/tokio core** -- HTTP parsing, connection management, and I/O are handled in Rust, eliminating Python GIL contention for these operations
- **Native HTTP/2** -- HTTP/2 support without requiring an external reverse proxy or TLS termination
- **Per-worker runtime threads** -- Each worker can run multiple tokio runtime threads for parallel I/O within a single process
- **Low latency** -- Sub-millisecond overhead per request

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.server.type` | `str` | `"auto"` | Set to `"granian"` to force Granian |
| `pyfly.server.workers` | `int` | `1` | Worker processes (`0` also resolves to 1) |
| `pyfly.server.backlog` | `int` | `1024` | TCP listen backlog |
| `pyfly.server.graceful-timeout` | `int` | `30` | Graceful shutdown timeout in seconds |
| `pyfly.server.http` | `str` | `"auto"` | HTTP version (`auto`, `1`, `2`) |
| `pyfly.server.keep-alive-timeout` | `int` | `5` | Keep-alive timeout in seconds |
| `pyfly.server.granian.runtime-threads` | `int` | `1` | Tokio runtime threads per worker |
| `pyfly.server.granian.runtime-mode` | `str` | `"auto"` | Runtime mode: `auto`, `st`, `mt` |

### Granian-Specific Properties

```yaml
pyfly:
  server:
    type: granian
    workers: 4
    granian:
      runtime-threads: 2       # Tokio threads per worker
      runtime-mode: "auto"     # auto | st (single-thread) | mt (multi-thread)
```

**`runtime-threads`**: Number of tokio runtime threads per worker process. Increasing this allows parallel I/O within a single worker. Default is `1`, which is optimal for most CPU-bound Python workloads. Increase for I/O-heavy workloads with many concurrent connections.

**`runtime-mode`**: Controls the tokio runtime mode. `st` (single-thread) uses a single-threaded runtime; `mt` (multi-thread) uses a multi-threaded runtime with work-stealing. `auto` selects based on the `runtime-threads` value (`st` for 1 thread, `mt` for multiple).

---

## Auto-Detection

Granian is auto-selected when installed because it has the highest priority in the server auto-configuration cascade:

1. `ServerAutoConfiguration.granian_server` bean -- `@conditional_on_class("granian")` + `@conditional_on_missing_bean(ApplicationServerPort)`
2. `ServerAutoConfiguration.uvicorn_server` bean -- only activated if Granian is not installed
3. `ServerAutoConfiguration.hypercorn_server` bean -- only activated if neither Granian nor Uvicorn is installed

To force Granian explicitly (even when other servers are installed):

```yaml
pyfly:
  server:
    type: granian
```

Or via CLI:

```bash
pyfly run --server granian
```

---

## Benchmarks

Approximate single-worker throughput on plain-text "Hello, World" responses (AMD Ryzen 9, Linux 6.x):

| Server | Requests/sec | Relative |
|--------|-------------|----------|
| Granian | ~112,000 | 1.0x (baseline) |
| Uvicorn (httptools) | ~37,000 | 0.33x |
| Hypercorn | ~12,000 | 0.11x |

These numbers are indicative. Real-world performance depends on your application's I/O patterns, serialization overhead, and database latency. The Rust-level HTTP parsing in Granian provides the most benefit for high-concurrency, low-latency workloads.

---

## When to Use Granian

**Choose Granian when:**

- You need the highest possible throughput for production APIs
- Your workload is I/O-bound with many concurrent connections
- You want native HTTP/2 without a reverse proxy
- You are deploying on Linux or macOS (Granian's primary targets)

**Consider Uvicorn instead when:**

- You need `--reload` for development (Granian supports reload, but Uvicorn's is more mature)
- You need maximum ecosystem compatibility
- You are running in an environment where Rust compilation is not available

**Consider Hypercorn instead when:**

- You need HTTP/3 (QUIC) support
- You need Trio event loop support

---

## See Also

- [Server Module Guide](../modules/server.md) -- Full server architecture reference
- [Starlette Adapter](starlette.md) -- Web framework adapter
- [FastAPI Adapter](fastapi.md) -- FastAPI web framework adapter
- [Adapter Catalog](README.md)
