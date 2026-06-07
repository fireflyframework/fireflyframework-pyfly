# PyFly benchmarks

Dependency-free micro-benchmarks for representative hot paths. **Not part of CI** — run
manually to spot regressions and quantify the overhead PyFly *itself* adds.

```bash
uv run python benchmarks/run.py
```

## Methodology (so the ratios are trustworthy)

- **Measures the framework, not the test harness.** Request benchmarks drive the ASGI app
  **directly** (build a scope, drive `receive`/`send` on one shared event loop). They do **not**
  use `TestClient`: a TestClient round-trip is ~500 µs/req dominated by httpx + the test
  transport + a per-request portal, so measuring through it computes PyFly's overhead against a
  ~100× inflated base. Real Starlette ASGI handling is ~5 µs/req.
- **Doesn't attribute dependencies to PyFly.** Rows tagged `[dep]` measure a third-party library
  (e.g. Pydantic v2), not PyFly code; `[base]` is a reference baseline.
- **Quantifies noise.** Each benchmark warms up, then runs 9 timed passes with the GC disabled
  during measurement; we report median µs/op, best, p99, and the run-to-run spread (`±%`).

Absolute numbers are machine-dependent — watch the **ratios** and the **±%** over time. Sample
run (Apple Silicon, Python 3.12):

| benchmark                                       | median µs/op | ops/sec |  ±%  |
|-------------------------------------------------|-------------:|--------:|-----:|
| container.resolve singleton (cached)            |        ~0.19 |   ~5.3M |  ~2% |
| container.resolve transient (no deps)           |        ~2.37 |   ~423K |  ~1% |
| container.resolve transient + 1 dep             |        ~2.95 |   ~339K |  ~3% |
| container.resolve transient + 3 deps            |        ~3.79 |   ~264K |  ~1% |
| container.resolve transient + 5 deps            |        ~4.59 |   ~218K |  ~1% |
| container.resolve transient + 10 deps           |        ~6.52 |   ~153K |  ~1% |
| container.resolve nested depth-3 (transient)    |       ~10.55 |    ~95K |  ~1% |
| pydantic v2 model_dump_json `[dep]`             |        ~0.67 |   ~1.5M |  ~1% |
| bare Starlette ASGI (real baseline) `[base]`    |        ~4.80 |   ~208K |  ~2% |
| pyfly filter chain (access log off)             |       ~47.9  |    ~21K |  ~1% |

## DI container — how it scales

Singleton resolution is effectively free (cached, ~0.19 µs). Transient resolution scales
**linearly** with the dependency count — roughly **+0.4 µs per dependency** (1→2.95, 3→3.79,
5→4.59, 10→6.52 µs): no superlinear or hidden per-bean cost. Depth costs the same as width per
node (a depth-3 all-transient graph ≈ resolving ~3 nested transients). Each transient still pays
its own `_resolve_param` + construction — that cost is irreducible.

> Historical note: transient-with-deps was ~15.6 µs (~64K ops/s) before v26.06.63, dominated by
> `typing.get_type_hints` + `inspect.signature` running on *every* resolve. v26.06.63 caches the
> constructor injection plan at registration and computes `get_origin` once per parameter — a
> ~5× speedup, now reflected above.

## Request overhead — what PyFly's filter chain costs

Against the **real** bare-ASGI baseline (~4.8 µs), the full default chain (access log off) costs
~48 µs/req → **+~43 µs/req of framework CPU overhead**, ~21K req/s through the chain. The chain
*machinery* is free (the filter-chain middleware with zero filters measures within noise of bare
Starlette); the cost is the per-filter features. Per-filter decomposition (direct ASGI, access
log excluded — it is I/O, see below):

| filter | +µs/req | notes |
|--------|--------:|-------|
| RequestContextFilter   | ~6.2 | request id + contextvar (needed for REQUEST scope / method security) |
| CorrelationFilter      | ~8.8 | correlation id propagation |
| TracingFilter          | ~6.3 | opens a server span (only when OpenTelemetry is installed) |
| TransactionIdFilter    | ~3.7 | propagates/generates an **`X-Transaction-Id`** correlation id (MDC-style request id for log/trace correlation — **not** declarative `@Transactional` transaction management) |
| SecurityHeadersFilter  | ~1.0 | OWASP headers (precomputed + bulk-appended since v26.06.64) |

The **access log** (`RequestLoggingFilter`) is deliberately excluded from the CPU number: its
cost is structured-log *render + I/O*, which is sink-dependent, not a CPU hot path. It is opt-out
via `pyfly.web.request-logging.enabled=false`. With a typical sink it adds on the order of tens of
µs/req — measure it in your own environment against your real log sink rather than trusting a
microbenchmark number.
