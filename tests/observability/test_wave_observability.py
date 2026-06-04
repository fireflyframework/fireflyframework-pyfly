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
"""Regression tests for #153 — TracerProvider span processor / exporter wiring."""

from __future__ import annotations

import importlib.util

from opentelemetry.sdk.trace import TracerProvider

from pyfly.core.config import Config
from pyfly.observability.auto_configuration import TracingAutoConfiguration

try:
    _OTLP_AVAILABLE = importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http.trace_exporter") is not None
except ModuleNotFoundError:
    _OTLP_AVAILABLE = False


def _processors(provider: TracerProvider) -> tuple:
    return getattr(provider._active_span_processor, "_span_processors", ())


class TestSpanProcessorWiring:
    def test_console_exporter_wires_processor(self):
        provider = TracerProvider()
        TracingAutoConfiguration._install_span_processor(
            provider, Config({"pyfly": {"observability": {"tracing": {"exporter": "console"}}}})
        )
        assert len(_processors(provider)) == 1

    def test_no_exporter_wires_nothing(self):
        provider = TracerProvider()
        TracingAutoConfiguration._install_span_processor(provider, Config({}))
        assert len(_processors(provider)) == 0

    def test_otlp_endpoint_autoselects_without_raising(self, monkeypatch):
        # An OTLP endpoint in the environment selects the OTLP exporter, gracefully
        # skipping (not crashing) when the exporter package is not installed.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        provider = TracerProvider()
        TracingAutoConfiguration._install_span_processor(provider, Config({}))
        assert len(_processors(provider)) == (1 if _OTLP_AVAILABLE else 0)

    def test_tracer_provider_bean_installs_processor(self):
        provider = TracingAutoConfiguration().tracer_provider(
            Config({"pyfly": {"observability": {"tracing": {"exporter": "console"}}}})
        )
        assert len(_processors(provider)) == 1
