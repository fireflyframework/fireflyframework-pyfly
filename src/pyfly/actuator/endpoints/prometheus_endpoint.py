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
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
except ImportError:
    CONTENT_TYPE_LATEST = ""
    generate_latest = None  # type: ignore[assignment]


class PrometheusEndpoint:
    """Endpoint at ``/actuator/prometheus`` — Prometheus scrape target.

    Returns metrics in Prometheus text exposition format.
    """

    @property
    def endpoint_id(self) -> str:
        return "prometheus"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        output = generate_latest().decode("utf-8")
        return {
            "content_type": CONTENT_TYPE_LATEST,
            "body": output,
        }
