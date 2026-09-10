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

import pytest
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


class TestOtlpEndpointNormalisation:
    """``OTEL_EXPORTER_OTLP_ENDPOINT`` is a BASE url; the exporter's ``endpoint=`` is a FULL one.

    The OpenTelemetry specification is explicit: ``OTEL_EXPORTER_OTLP_ENDPOINT`` is a base to which the
    SDK appends the per-signal path, while ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` — and the exporter's
    own ``endpoint=`` argument — is the complete URL, used verbatim. Reading the base variable and
    handing it straight to ``OTLPSpanExporter(endpoint=...)`` collapsed the two: an operator who set the
    spec-correct ``http://collector:4318`` got an exporter POSTing to ``http://collector:4318``, which
    is not a signal endpoint, and every span was dropped with nothing logged. The only way to make it
    work was to set the base variable to a value the spec says is not a base.

    Both spellings must now work, from either source.
    """

    @staticmethod
    def _endpoint_of(provider) -> str:
        processor = _processors(provider)[0]
        return processor.span_exporter._endpoint

    @pytest.mark.skipif(not _OTLP_AVAILABLE, reason="opentelemetry-exporter-otlp is not installed")
    def test_a_base_endpoint_gains_the_signal_path(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        provider = TracerProvider()
        TracingAutoConfiguration._install_span_processor(provider, Config({}))

        assert self._endpoint_of(provider) == "http://collector:4318/v1/traces"

    @pytest.mark.skipif(not _OTLP_AVAILABLE, reason="opentelemetry-exporter-otlp is not installed")
    def test_a_base_endpoint_with_a_trailing_slash_gains_it_once(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/")
        provider = TracerProvider()
        TracingAutoConfiguration._install_span_processor(provider, Config({}))

        assert self._endpoint_of(provider) == "http://collector:4318/v1/traces"

    @pytest.mark.skipif(not _OTLP_AVAILABLE, reason="opentelemetry-exporter-otlp is not installed")
    def test_a_full_signal_endpoint_is_left_alone(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/v1/traces")
        provider = TracerProvider()
        TracingAutoConfiguration._install_span_processor(provider, Config({}))

        assert self._endpoint_of(provider) == "http://collector:4318/v1/traces"

    @pytest.mark.skipif(not _OTLP_AVAILABLE, reason="opentelemetry-exporter-otlp is not installed")
    def test_the_pyfly_config_key_is_normalised_the_same_way(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        provider = TracerProvider()
        TracingAutoConfiguration._install_span_processor(
            provider,
            Config({"pyfly": {"observability": {"tracing": {"otlp": {"endpoint": "http://collector:4318"}}}}}),
        )

        assert self._endpoint_of(provider) == "http://collector:4318/v1/traces"
