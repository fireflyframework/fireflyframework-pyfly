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
"""PyFly data-layer benchmarks: unit-of-work cost, statements per repository method, pool reuse.

Run from the repository root::

    uv run python benchmarks/data/run.py                          # sqlite-file, every scenario
    uv run python benchmarks/data/run.py --backend pg             # PostgreSQL 17 in a testcontainer
    uv run python benchmarks/data/run.py --backend mysql --scenario p6 p7
    uv run python benchmarks/data/run.py --backend pg --server-url postgresql+asyncpg://u:p@host:5432/postgres
    uv run python benchmarks/data/run.py --backend pg --json results.json

Backends are the lanes of the test backend matrix (``tests/support/backend_matrix.py``): ``sqlite-file``,
``pg``, ``mysql`` and ``mariadb``. A server lane starts its container through testcontainers (Docker
required; set ``TESTCONTAINERS_RYUK_DISABLED=true`` where Ryuk cannot run) and removes it afterwards,
unless ``--server-url`` (or the lane's ``PYFLY_IT_*`` variable) names a server to use instead. The
URL must be allowed to create databases. ``--server-url`` is an async SQLAlchemy URL and is used as
given, driver included; a ``PYFLY_IT_*`` URL gets the lane's async driver, as in the test matrix.
Every scenario gets a database of its own, dropped at the end.

A scenario that raises is recorded in the report as ``{"error": "<repr of the exception>"}``, with its
traceback on stderr. The remaining scenarios still run, the ``--json`` file is still written, and the
exit status is 1.

What is measured, and why, is described in ``benchmarks/data/scenarios.py``; the recorded numbers
are in ``benchmarks/data/BASELINE.md``. Latencies are wall-clock times on one event loop, medians
over many calls; compare them on the same machine, and read the statement counts as exact.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import json
import logging
import platform
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))  # the backend matrix helpers live in tests/support

import sqlalchemy  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from benchmarks.data.scenarios import SCENARIOS, boot  # noqa: E402
from tests.support.backend_matrix import (  # noqa: E402
    RELATIONAL_LANES,
    SERVER_SPECS,
    SQLITE_FILE,
    RunningServer,
    create_database,
    drop_database,
    new_database_name,
    start_server,
)


@contextlib.contextmanager
def _server(lane: str, server_url: str | None) -> Iterator[RunningServer | None]:
    if lane == SQLITE_FILE:
        yield None
        return
    server = RunningServer(lane, server_url) if server_url else start_server(lane)
    try:
        yield server
    finally:
        server.stop()


async def _server_version(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            if engine.dialect.name == "sqlite":
                return f"SQLite {sqlite3.sqlite_version}"
            if engine.dialect.name == "postgresql":
                return f"PostgreSQL {(await conn.execute(text('SHOW server_version'))).scalar_one()}"
            return str((await conn.execute(text("SELECT VERSION()"))).scalar_one())
    finally:
        await engine.dispose()


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def _run_scenario(
    name: str, lane: str, server: RunningServer | None, workdir: Path, meta: dict[str, Any]
) -> dict[str, Any]:
    """Run scenario *name* on a database of its own, created here and dropped afterwards."""
    if server is None:
        url = f"sqlite+aiosqlite:///{workdir / f'{name}.db'}"
        database = None
    else:
        database = new_database_name()
        url = await create_database(server.url, database)
    try:
        if "server" not in meta:
            meta["server"] = await _server_version(url)
            meta["driver"] = sqlalchemy.engine.make_url(url).get_driver_name()
            print(f"[{lane}] {meta['server']} via {meta['driver']}", flush=True)
        print(f"[{name}]", flush=True)
        env = await boot(lane, url)
        try:
            return await SCENARIOS[name](env)
        finally:
            await env.context.stop()
            del env
            gc.collect()  # finalize pinned sessions while the loop still runs
    finally:
        if server is not None and database is not None:
            await drop_database(server.url, database)


async def _run(lane: str, server: RunningServer | None, scenarios: list[str], workdir: Path) -> dict[str, Any]:
    """Run *scenarios* in order. A scenario that raises is recorded as ``{"error": repr(exc)}`` and
    the run goes on, so one broken scenario cannot hide the others or the ``--json`` report."""
    results: dict[str, Any] = {}
    meta: dict[str, Any] = {"backend": lane}
    for name in scenarios:
        try:
            results[name] = await _run_scenario(name, lane, server, workdir, meta)
        except Exception as exc:
            results[name] = {"error": repr(exc)}
            print(f"    FAILED: {exc!r}", flush=True)
            traceback.print_exc()
    return {"meta": meta, "scenarios": results}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the harness; the exit status is 1 when any scenario failed (its error is in the report)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", choices=RELATIONAL_LANES, default=SQLITE_FILE)
    parser.add_argument(
        "--server-url",
        help=(
            "a server to use instead of starting a container (server lanes): an async SQLAlchemy URL, "
            "used as given (e.g. postgresql+asyncpg://user:pass@host:5432/postgres), whose account may "
            "create and drop databases"
        ),
    )
    parser.add_argument("--scenario", nargs="+", choices=list(SCENARIOS), default=list(SCENARIOS))
    parser.add_argument("--json", type=Path, help="also write the results to this JSON file")
    args = parser.parse_args(argv)

    logging.disable(logging.WARNING)  # the context logs its lifecycle at INFO; keep the output to results
    started = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as workdir, _server(args.backend, args.server_url) as server:
        if server is not None:
            image = SERVER_SPECS[args.backend].image if server.container is not None else "external server"
            print(f"[{args.backend}] server: {image}", flush=True)
        report = asyncio.run(_run(args.backend, server, args.scenario, Path(workdir)))

    report["meta"].update(
        {
            "date": started.isoformat(timespec="seconds"),
            "commit": _commit(),
            "python": platform.python_version(),
            "sqlalchemy": sqlalchemy.__version__,
            "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
        }
    )
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(f"results written to {args.json}")
    failed = [name for name, result in report["scenarios"].items() if "error" in result]
    if failed:
        print(f"failed scenarios: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
