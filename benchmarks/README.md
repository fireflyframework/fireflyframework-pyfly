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
| container.resolve (singleton, cached)       |    ~5.3M |  0.19 |
| container.resolve (transient, construct)    |    ~420K |  2.38 |
| container.resolve (transient + 1 dep)       |     ~64K | 15.55 |
| pydantic model_dump_json                    |    ~1.4M |  0.69 |
| pyfly GET vs bare Starlette                 |      —   | ~+26% |

Notes:
- Singleton resolution is effectively free (cached). Transient-with-deps is dominated by
  per-resolve `typing.get_type_hints` — a candidate for a cached-hints optimization.
- The PyFly filter chain (request context, correlation, tracing, logging, security headers)
  adds ~26% over a bare Starlette route — the cost of the enterprise middleware.
