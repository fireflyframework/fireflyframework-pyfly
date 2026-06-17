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
"""prometheus_client multiprocess-mode support for multi-worker deployments.

When a pyfly app runs with ``workers > 1`` (``uvicorn.run(workers=N)``,
Granian/Hypercorn pre-fork), each worker is a separate process with its own
default Prometheus ``REGISTRY``. A single ``/actuator/prometheus`` scrape would
then reflect only the one worker that answered. prometheus_client's multiprocess
mode fixes this: each worker writes its metric values to mmap files in a shared
directory, and the scrape aggregates across all of them via
``MultiProcessCollector``.

The directory MUST be set in ``PROMETHEUS_MULTIPROC_DIR`` **before the first
metric is created** in every worker. :func:`init_multiprocess_dir` is therefore
called from ``pyfly run`` *before* the server forks workers, so the env var is
inherited by every worker process.

Limitation: custom Python collectors (e.g. the process/system metrics collector)
are NOT aggregated by ``MultiProcessCollector`` — only ``Counter`` / ``Gauge`` /
``Histogram`` / ``Summary`` values written to the mmap files are. The ``server_*``
and ``http_server_requests_*`` meters are real Counters/Gauges, so they aggregate
correctly; process gauges fall back to single-process semantics.
"""

from __future__ import annotations

import atexit
import contextlib
import glob
import os
import shutil
import tempfile
from typing import Any

_ENV = "PROMETHEUS_MULTIPROC_DIR"
_DIR_PREFIX = "pyfly-prometheus-mp-"


def is_multiprocess() -> bool:
    """True when prometheus_client multiprocess mode is active."""
    return bool(os.environ.get(_ENV))


def init_multiprocess_dir(workers: int) -> str | None:
    """Create + register a multiprocess metrics dir for a multi-worker run.

    Must be called BEFORE the server forks workers and before any metric is
    created. No-ops (returns ``None``) for a single worker, or returns the
    pre-existing dir if ``PROMETHEUS_MULTIPROC_DIR`` is already set by the
    operator. The fresh dir is cleared so stale series from a previous run of the
    same launcher pid do not leak into the new run.
    """
    if workers <= 1:
        return None
    existing = os.environ.get(_ENV)
    if existing:
        return existing
    _sweep_stale_dirs()
    path = os.path.join(tempfile.gettempdir(), f"{_DIR_PREFIX}{os.getpid()}")
    os.makedirs(path, exist_ok=True)
    _clear_dir(path)
    os.environ[_ENV] = path
    # Clean up our own dir on launcher exit so mmap files don't accumulate in tmp
    # across restarts. Only the launcher (which created the dir) registers this;
    # forked workers inherit the env var but not this atexit hook.
    _launcher_pid = os.getpid()

    def _cleanup() -> None:  # pragma: no cover - runs at interpreter exit
        if os.getpid() == _launcher_pid:
            shutil.rmtree(path, ignore_errors=True)

    atexit.register(_cleanup)
    return path


def _clear_dir(path: str) -> None:
    for db_file in glob.glob(os.path.join(path, "*.db")):
        with contextlib.suppress(OSError):
            os.remove(db_file)


def _sweep_stale_dirs() -> None:
    """Remove leftover pyfly multiprocess dirs from crashed prior launches."""
    pattern = os.path.join(tempfile.gettempdir(), f"{_DIR_PREFIX}*")
    for stale in glob.glob(pattern):
        with contextlib.suppress(OSError):
            shutil.rmtree(stale, ignore_errors=True)


def build_multiprocess_registry() -> Any:
    """A fresh registry that aggregates every worker's mmap-backed metrics.

    Use this (instead of the default ``REGISTRY``) to scrape in multiprocess mode.
    """
    from prometheus_client import CollectorRegistry
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    return registry


def collect_registry() -> Any:
    """Return the registry to scrape: aggregating in multiprocess mode, else default.

    Tolerant: if ``PROMETHEUS_MULTIPROC_DIR`` is set but the directory is missing
    or unreadable, it is (re)created when possible and otherwise falls back to the
    process default ``REGISTRY`` rather than letting the scrape raise a 500.
    """
    from prometheus_client import REGISTRY

    if not is_multiprocess():
        return REGISTRY
    try:
        path = os.environ.get(_ENV)
        if path and not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
        return build_multiprocess_registry()
    except Exception:  # noqa: BLE001 - never let multiprocess setup break the scrape
        return REGISTRY


def mark_worker_dead(pid: int) -> None:
    """Drop a dead worker's live-gauge contributions (call on worker exit)."""
    if not is_multiprocess():
        return
    with contextlib.suppress(Exception):
        from prometheus_client.multiprocess import mark_process_dead

        mark_process_dead(pid)  # type: ignore[no-untyped-call]
