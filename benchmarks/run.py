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
"""PyFly micro-benchmarks — DI container, serialization, and request overhead.

Goal: measure what PyFly actually adds on the hot paths and catch regressions — not produce
flattering numbers. Three principles drive the design:

1. **Measure the framework, not the harness.** Request benchmarks drive the ASGI app DIRECTLY
   (build a scope, drive receive/send on a single shared event loop). We deliberately do NOT use
   ``TestClient`` for the headline numbers: a TestClient round-trip is ~500µs/req dominated by
   httpx + the test transport + a per-request portal — so "bare Starlette via TestClient" looks
   like ~hundreds of µs when real Starlette ASGI handling is ~5µs. Measuring through TestClient
   would compute PyFly's overhead against a ~100x inflated base. We report PyFly's overhead in
   absolute µs/req AND as a ratio over the *real* bare-ASGI baseline.

2. **Don't attribute dependencies to PyFly.** ``pydantic model_dump_json`` is essentially
   Pydantic v2, not PyFly — it's labelled ``[dep]`` (dependency baseline) for tracking only.

3. **Quantify the noise.** Each benchmark runs warmup + several timed runs with the GC disabled
   during measurement; we report median µs/op and the p99/spread across runs, because the
   "watch the ratios over time" guidance only works if the ratios are stable.

Run with::

    uv run python benchmarks/run.py
"""

from __future__ import annotations

import gc
import statistics
import time
from collections.abc import Callable
from typing import Any

# Number of timed runs per benchmark (median + spread reported across them).
RUNS = 9


# --------------------------------------------------------------------------- measurement core
class Result:
    def __init__(self, name: str, samples_us: list[float], iterations: int, tag: str = "") -> None:
        self.name = name
        self.tag = tag  # "" (PyFly), "dep" (dependency baseline), "base" (reference)
        self.iterations = iterations
        self.median = statistics.median(samples_us)
        self.best = min(samples_us)
        self.p99 = max(samples_us) if len(samples_us) < 100 else statistics.quantiles(samples_us, n=100)[98]
        self.stdev_pct = (statistics.pstdev(samples_us) / self.median * 100) if self.median else 0.0

    @property
    def ops(self) -> float:
        return 1_000_000 / self.median if self.median else float("inf")


def _bench(name: str, fn: Callable[[], Any], iterations: int, *, tag: str = "", runs: int = RUNS) -> Result:
    """Run *fn* ``iterations`` times per run, ``runs`` runs, GC disabled while timing."""
    for _ in range(max(1, iterations // 5)):  # warmup: prime imports/caches/contextvars
        fn()
    samples: list[float] = []
    for _ in range(runs):
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            for _ in range(iterations):
                fn()
            elapsed = time.perf_counter() - start
        finally:
            gc.enable()
        samples.append(elapsed / iterations * 1_000_000)
    return Result(name, samples, iterations, tag=tag)


async def _abench(name: str, factory: Callable[[], Any], iterations: int, *, tag: str = "", runs: int = RUNS) -> Result:
    """Async variant: ``factory()`` returns a fresh awaitable each call."""
    for _ in range(max(1, iterations // 5)):
        await factory()
    samples: list[float] = []
    for _ in range(runs):
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            for _ in range(iterations):
                await factory()
            elapsed = time.perf_counter() - start
        finally:
            gc.enable()
        samples.append(elapsed / iterations * 1_000_000)
    return Result(name, samples, iterations, tag=tag)


_TAG_LABEL = {"": "", "dep": "  [dep]", "base": "  [base]"}


def _print(rows: list[Result]) -> None:
    print(f"\n{'benchmark':<48}{'median µs/op':>13}{'best':>9}{'p99':>9}{'±%':>7}{'ops/sec':>13}")
    print("-" * 99)
    for r in rows:
        label = r.name + _TAG_LABEL.get(r.tag, "")
        print(f"{label:<48}{r.median:>13.3f}{r.best:>9.3f}{r.p99:>9.3f}{r.stdev_pct:>6.0f}%{r.ops:>13,.0f}")
    print(
        "\n[dep] = third-party dependency baseline (NOT PyFly code).  "
        "[base] = reference baseline.  ±% = run-to-run spread."
    )


# --------------------------------------------------------------------------- DI container beans
# Module-level (so typing.get_type_hints resolves them). Ten distinct leaf types let us vary the
# dependency COUNT, and a linear chain lets us vary dependency DEPTH.
class A1:
    pass


class A2:
    pass


class A3:
    pass


class A4:
    pass


class A5:
    pass


class A6:
    pass


class A7:
    pass


class A8:
    pass


class A9:
    pass


class A10:
    pass


class Deps1:
    def __init__(self, a1: A1) -> None: ...


class Deps3:
    def __init__(self, a1: A1, a2: A2, a3: A3) -> None: ...


class Deps5:
    def __init__(self, a1: A1, a2: A2, a3: A3, a4: A4, a5: A5) -> None: ...


class Deps10:
    def __init__(self, a1: A1, a2: A2, a3: A3, a4: A4, a5: A5, a6: A6, a7: A7, a8: A8, a9: A9, a10: A10) -> None: ...


class Lvl1:
    def __init__(self, a1: A1) -> None: ...


class Lvl2:
    def __init__(self, x: Lvl1) -> None: ...


class Lvl3:
    def __init__(self, x: Lvl2) -> None: ...


def _di_benchmarks() -> list[Result]:
    from pyfly.container.container import Container
    from pyfly.container.types import Scope

    leaves = [A1, A2, A3, A4, A5, A6, A7, A8, A9, A10]

    singleton = Container()
    singleton.register(A1, scope=Scope.SINGLETON)
    transient = Container()
    transient.register(A1, scope=Scope.TRANSIENT)

    # Width: transient target with N singleton (cached) leaves — isolates per-dependency
    # resolution cost (not leaf construction). Expect cost ~linear in N.
    width = Container()
    for leaf in leaves:
        width.register(leaf, scope=Scope.SINGLETON)
    for target in (Deps1, Deps3, Deps5, Deps10):
        width.register(target, scope=Scope.TRANSIENT)

    # Depth: a fully-transient chain Lvl3 -> Lvl2 -> Lvl1 -> A1 (every level constructed).
    depth = Container()
    for cls in (A1, Lvl1, Lvl2, Lvl3):
        depth.register(cls, scope=Scope.TRANSIENT)

    return [
        _bench("container.resolve singleton (cached)", lambda: singleton.resolve(A1), 200_000),
        _bench("container.resolve transient (no deps)", lambda: transient.resolve(A1), 200_000),
        _bench("container.resolve transient + 1 dep", lambda: width.resolve(Deps1), 100_000),
        _bench("container.resolve transient + 3 deps", lambda: width.resolve(Deps3), 100_000),
        _bench("container.resolve transient + 5 deps", lambda: width.resolve(Deps5), 100_000),
        _bench("container.resolve transient + 10 deps", lambda: width.resolve(Deps10), 50_000),
        _bench("container.resolve nested depth-3 (transient)", lambda: depth.resolve(Lvl3), 50_000),
    ]


def _serialization_benchmarks() -> list[Result]:
    from pydantic import BaseModel

    class _User(BaseModel):
        id: int
        name: str
        email: str
        roles: list[str]

    user = _User(id=1, name="Ada", email="ada@example.com", roles=["ADMIN", "USER"])
    # Tagged [dep]: this measures Pydantic v2, not PyFly — kept only to track the dependency.
    return [_bench("pydantic v2 model_dump_json", lambda: user.model_dump_json(), 200_000, tag="dep")]


# --------------------------------------------------------------------------- request (direct ASGI)
def _http_scope() -> dict[str, Any]:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/hi",
        "raw_path": b"/hi",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
    }


def _make_receive() -> Callable[[], Any]:
    state = {"done": False}

    async def receive() -> dict[str, Any]:
        if not state["done"]:
            state["done"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _drive(app: Any, scope: dict[str, Any]) -> None:
    async def send(_message: dict[str, Any]) -> None:
        pass

    await app(dict(scope), _make_receive(), send)  # fresh scope/receive per call


async def _request_benchmarks() -> list[Result]:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.web.adapters.starlette.app import create_app

    async def hello(_req: Any) -> JSONResponse:
        return JSONResponse({"message": "hi"})

    bare = Starlette(routes=[Route("/hi", hello)])
    # Access log OFF: the chain's cost is then pure CPU (request context, correlation, tracing
    # span, transaction id, security headers + the buffering middleware), which is reproducible.
    # The access log itself is sink-dependent I/O (not a CPU hot path) and opt-out, so it is
    # measured separately via the per-filter decomposition in benchmarks/README.md, not here.
    chain = create_app(
        context=ApplicationContext(Config({"pyfly": {"web": {"request-logging": {"enabled": "false"}}}})),
        extra_routes=[Route("/hi", hello)],
    )
    scope = _http_scope()

    rows = [
        await _abench("bare Starlette ASGI (real baseline)", lambda: _drive(bare, scope), 20_000, tag="base"),
        await _abench("pyfly filter chain (access log off)", lambda: _drive(chain, scope), 20_000),
    ]
    base = rows[0].median
    overhead = rows[1].median - base
    print(
        f"\nFilter-chain CPU overhead vs the REAL bare-ASGI baseline: +{overhead:.1f} µs/req over a "
        f"~{base:.1f}µs base (~{rows[1].ops:,.0f} req/s through the full chain).\n"
        f"The access log is sink-dependent I/O (opt-out via pyfly.web.request-logging.enabled) and is\n"
        f"excluded from this CPU number; see benchmarks/README.md for its per-filter breakdown.\n"
        f"NOTE: a TestClient round-trip is ~500µs/req (httpx + test transport) — harness cost, NOT\n"
        f"framework overhead — and is intentionally excluded from every number here."
    )
    return rows


def main() -> None:
    import asyncio
    import logging

    logging.disable(logging.INFO)  # quiet stdlib loggers (the access log is handled via fd redirect)

    rows: list[Result] = []
    rows += _di_benchmarks()
    rows += _serialization_benchmarks()
    rows += asyncio.run(_request_benchmarks())
    _print(rows)


if __name__ == "__main__":
    main()
