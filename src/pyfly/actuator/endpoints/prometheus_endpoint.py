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
"""Prometheus actuator endpoint — exposes metrics in text exposition format."""

from __future__ import annotations

from typing import Any

try:
    from prometheus_client import generate_latest
except ImportError:
    generate_latest = None  # type: ignore[assignment]

# Spring Boot / Micrometer serve the classic Prometheus text exposition format
# (``version=0.0.4``). prometheus_client now defaults ``CONTENT_TYPE_LATEST`` to
# the OpenMetrics ``version=1.0.0``; pin the Spring-compatible value so existing
# Prometheus scrapers and Spring tooling consume it unchanged.
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class PrometheusEndpoint:
    """Endpoint at ``/actuator/prometheus`` — Prometheus scrape target.

    Returns metrics in Prometheus text exposition format.
    """

    @property
    def endpoint_id(self) -> str:
        return "prometheus"

    @property
    def enabled(self) -> bool:
        # Only active when prometheus_client is importable (audit #162).
        return generate_latest is not None

    async def handle(self, context: Any = None) -> dict[str, Any]:
        if generate_latest is None:
            # Defensive: the endpoint should not be registered without
            # prometheus_client, but never raise an unconverted TypeError at
            # request time if it is (audit #162).
            return {
                "content_type": "text/plain; charset=utf-8",
                "body": "# prometheus_client is not installed\n",
                "status": 503,
            }
        from pyfly.observability.multiprocess import build_multiprocess_registry, is_multiprocess

        # Under multiprocess mode (workers > 1) aggregate every worker's
        # mmap-backed metrics; otherwise scrape the process default registry.
        if is_multiprocess():
            output = generate_latest(build_multiprocess_registry()).decode("utf-8")
        else:
            output = generate_latest().decode("utf-8")
        return {
            "content_type": _CONTENT_TYPE,
            "body": output,
        }
