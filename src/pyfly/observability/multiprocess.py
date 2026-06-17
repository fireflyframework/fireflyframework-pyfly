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

import contextlib
import glob
import os
import tempfile
from typing import Any

_ENV = "PROMETHEUS_MULTIPROC_DIR"


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
    path = os.path.join(tempfile.gettempdir(), f"pyfly-prometheus-mp-{os.getpid()}")
    os.makedirs(path, exist_ok=True)
    _clear_dir(path)
    os.environ[_ENV] = path
    return path


def _clear_dir(path: str) -> None:
    for db_file in glob.glob(os.path.join(path, "*.db")):
        with contextlib.suppress(OSError):
            os.remove(db_file)


def build_multiprocess_registry() -> Any:
    """A fresh registry that aggregates every worker's mmap-backed metrics.

    Use this (instead of the default ``REGISTRY``) to scrape in multiprocess mode.
    """
    from prometheus_client import CollectorRegistry
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    return registry


def mark_worker_dead(pid: int) -> None:
    """Drop a dead worker's live-gauge contributions (call on worker exit)."""
    if not is_multiprocess():
        return
    with contextlib.suppress(Exception):
        from prometheus_client.multiprocess import mark_process_dead

        mark_process_dead(pid)  # type: ignore[no-untyped-call]
