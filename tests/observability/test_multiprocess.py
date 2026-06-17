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
"""Tests for prometheus multiprocess-mode support."""

from __future__ import annotations

import os

import pytest

from pyfly.observability import multiprocess as mp


class TestIsMultiprocess:
    def test_false_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        assert mp.is_multiprocess() is False

    def test_true_when_env_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        assert mp.is_multiprocess() is True


class TestInitMultiprocessDir:
    def test_single_worker_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        assert mp.init_multiprocess_dir(1) is None
        assert "PROMETHEUS_MULTIPROC_DIR" not in os.environ

    def test_multi_worker_creates_and_sets_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        try:
            path = mp.init_multiprocess_dir(4)
            assert path is not None
            assert os.path.isdir(path)
            assert os.environ["PROMETHEUS_MULTIPROC_DIR"] == path
        finally:
            os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)

    def test_respects_preexisting_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        assert mp.init_multiprocess_dir(4) == str(tmp_path)


class TestMultiprocessAggregation:
    """Cross-process aggregation. prometheus_client fixes its value class at import
    time, so a metric must be created in a *fresh* process (with the env var set)
    to be mmap-backed. A child process writes the file; the parent aggregates it
    via ``MultiProcessCollector`` reading the shared dir — exactly the
    worker→scrape path."""

    @staticmethod
    def _write_metric_in_child(multiproc_dir: str, metric: str, value: int) -> None:
        import subprocess
        import sys

        env = dict(os.environ)
        env["PROMETHEUS_MULTIPROC_DIR"] = multiproc_dir
        script = (
            "from prometheus_client import Counter\n"
            f"Counter({metric!r}, 'x').inc({value})\n"
        )
        subprocess.run([sys.executable, "-c", script], check=True, env=env)

    def test_registry_aggregates_metric_written_by_another_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        self._write_metric_in_child(str(tmp_path), "pyfly_mp_test_events", 3)

        from prometheus_client import generate_latest

        exposition = generate_latest(mp.build_multiprocess_registry()).decode()
        assert "pyfly_mp_test_events_total" in exposition
        assert "3.0" in exposition

    async def test_prometheus_endpoint_uses_multiprocess_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        self._write_metric_in_child(str(tmp_path), "pyfly_mp_endpoint_events", 1)

        from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint

        result = await PrometheusEndpoint().handle()
        assert result.get("status") != 503
        assert "pyfly_mp_endpoint_events_total" in result["body"]
