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
- The PyFly filter chain (request context, correlation, tracing, logging, security headers)
  adds ~27% over a bare Starlette route — the cost of the enterprise middleware.
