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
"""Distributed tracing facade for the orchestration engine.

The implementation falls back to a no-op tracer when ``opentelemetry`` is not
installed, so the engine works without tracing as a hard dependency.
"""

from __future__ import annotations

import contextlib
from typing import Any

try:
    from opentelemetry import trace as _otel_trace  # type: ignore[import-not-found]

    _HAS_OTEL = True
except Exception:  # noqa: BLE001
    _otel_trace = None
    _HAS_OTEL = False


class OrchestrationTracer:
    """Thin OpenTelemetry-compatible tracer wrapper.

    When OTel is missing, every operation is a no-op so tracing can be a
    drop-in upgrade.
    """

    def __init__(self, service_name: str = "pyfly.orchestration") -> None:
        self._service_name = service_name
        self._tracer: Any = (
            _otel_trace.get_tracer(service_name) if _HAS_OTEL else None
        )

    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Any:
        """Start a new span; yields a span-like object (or ``None`` when disabled)."""
        if self._tracer is None:
            yield None
            return
        with self._tracer.start_as_current_span(name) as span:
            for k, v in attributes.items():
                try:
                    span.set_attribute(k, v)
                except Exception:  # noqa: BLE001
                    pass
            yield span

    def is_enabled(self) -> bool:
        return self._tracer is not None
