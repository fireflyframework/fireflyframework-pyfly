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

Dependency-free (stdlib ``time``), not part of CI. Run with::

    uv run python benchmarks/run.py

Reports ops/sec and µs/op for representative hot paths so regressions are visible and the
framework's overhead vs a bare Starlette app is quantified.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def _bench(name: str, fn: Callable[[], Any], iterations: int) -> tuple[str, int, float, float]:
    for _ in range(max(1, iterations // 10)):  # warmup
        fn()
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    ops = iterations / elapsed if elapsed else float("inf")
    return name, iterations, ops, (elapsed / iterations) * 1_000_000


def _print_table(rows: list[tuple[str, int, float, float]]) -> None:
    print(f"\n{'benchmark':<46} {'iters':>9} {'ops/sec':>14} {'µs/op':>10}")
    print("-" * 82)
    for name, iters, ops, us in rows:
        print(f"{name:<46} {iters:>9,} {ops:>14,.0f} {us:>10.3f}")


class _Svc:
    pass


class _WithDep:
    def __init__(self, svc: _Svc) -> None:
        self.svc = svc


def _di_benchmarks() -> list[tuple[str, int, float, float]]:
    from pyfly.container.container import Container
    from pyfly.container.types import Scope

    singleton = Container()
    singleton.register(_Svc, scope=Scope.SINGLETON)
    transient = Container()
    transient.register(_Svc, scope=Scope.TRANSIENT)
    with_dep = Container()
    with_dep.register(_Svc, scope=Scope.SINGLETON)
    with_dep.register(_WithDep, scope=Scope.TRANSIENT)

    return [
        _bench("container.resolve (singleton, cached)", lambda: singleton.resolve(_Svc), 200_000),
        _bench("container.resolve (transient, construct)", lambda: transient.resolve(_Svc), 200_000),
        _bench("container.resolve (transient + 1 dep)", lambda: with_dep.resolve(_WithDep), 100_000),
    ]


def _serialization_benchmarks() -> list[tuple[str, int, float, float]]:
    from pydantic import BaseModel

    class _User(BaseModel):
        id: int
        name: str
        email: str
        roles: list[str]

    user = _User(id=1, name="Ada", email="ada@example.com", roles=["ADMIN", "USER"])
    return [_bench("pydantic model_dump_json", lambda: user.model_dump_json(), 200_000)]


def _request_benchmarks() -> list[tuple[str, int, float, float]]:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def _hello(_request: Any) -> JSONResponse:
        return JSONResponse({"message": "hi"})

    bare = TestClient(Starlette(routes=[Route("/hi", _hello)]))

    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.web.adapters.starlette.app import create_app

    ctx = ApplicationContext(Config({}))
    # create_app builds the full middleware/filter chain; the extra_route exercises it.
    pyfly_app = create_app(context=ctx, extra_routes=[Route("/hi", _hello)])
    pyfly_client = TestClient(pyfly_app)

    return [
        _bench("bare Starlette GET /hi", lambda: bare.get("/hi"), 3_000),
        _bench("pyfly GET /hi (full filter chain)", lambda: pyfly_client.get("/hi"), 3_000),
    ]


def main() -> None:
    import logging

    logging.disable(logging.INFO)  # silence per-request framework logs during the request bench
    rows: list[tuple[str, int, float, float]] = []
    rows += _di_benchmarks()
    rows += _serialization_benchmarks()
    rows += _request_benchmarks()
    _print_table(rows)


if __name__ == "__main__":
    main()
