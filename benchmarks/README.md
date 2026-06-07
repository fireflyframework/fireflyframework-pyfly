# PyFly benchmarks

Dependency-free micro-benchmarks for representative hot paths. **Not part of CI** — run
manually to spot regressions and quantify framework overhead.

```bash
uv run python benchmarks/run.py
```

Covers:
- **DI container** — singleton (cached), transient (construct), transient + 1 dependency.
- **Serialization** — Pydantic `model_dump_json`.
- **Request overhead** — a GET through the full PyFly filter chain vs bare Starlette.

Sample run (Apple Silicon, Python 3.12; absolute numbers are machine-dependent — watch the
*ratios* over time):

| benchmark                                   |  ops/sec | µs/op |
|---------------------------------------------|---------:|------:|
| container.resolve (singleton, cached)       |    ~5.1M |  0.19 |
| container.resolve (transient, construct)    |    ~420K |  2.40 |
| container.resolve (transient + 1 dep)       |    ~342K |  2.92 |
| pydantic model_dump_json                    |    ~1.5M |  0.68 |
| pyfly GET vs bare Starlette                 |      —   | ~+27% |

Notes:
- Singleton resolution is effectively free (cached).
- **Transient-with-deps was ~15.55µs (~64K ops/s) before v26.06.63** — dominated by
  `typing.get_type_hints` + `inspect.signature` running on *every* resolve. v26.06.63 caches
  the constructor injection plan at registration (parsed once) and computes `get_origin` once
  per parameter, bringing it to ~2.92µs (~342K ops/s) — a **~5.3x** speedup. The residual cost
  is the actual per-resolve dependency resolution + object construction, which is irreducible
  for a transient bean.
- The PyFly filter chain adds ~22% over a bare Starlette route — the cost of the enterprise
  middleware. The chain *machinery itself is free* (the filter-chain middleware with **zero**
  filters measures within noise of bare Starlette); the overhead is entirely the per-filter
  features. Decomposition (µs/req added vs bare ~436µs):

  | filter | +µs/req | notes |
  |--------|--------:|-------|
  | RequestContextFilter   | ~11 | request id + contextvar (needed for REQUEST scope / method security) |
  | CorrelationFilter      | ~17 | correlation id |
  | TracingFilter          | ~13 | opens a server span (only when OpenTelemetry is installed) |
  | TransactionIdFilter    | ~10 | transaction id |
  | RequestLoggingFilter   | ~38 | **biggest** — structured access log per request |
  | SecurityHeadersFilter  | ~10 | OWASP headers (precomputed + bulk-appended since v26.06.64) |

  The access log is the single biggest cost, so it is opt-out: set
  `pyfly.web.request-logging.enabled=false` to drop ~38µs/req (overhead ~22% → ~13%).
