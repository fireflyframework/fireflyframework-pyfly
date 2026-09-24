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
"""No SERVER span for the actuator, and a MeterProvider beside the TracerProvider.

Two omissions a service feeding an OpenTelemetry chain had to patch at boot:

* ``TracingFilter`` opened a SERVER span for every request on both listeners, so kubelet
  probes at 1 Hz dominated the trace store, and the only way to exclude a path was to mutate the
  class attribute before ``create_app`` instantiated the filter.
* ``TracingAutoConfiguration`` declared a ``TracerProvider`` and nothing else, so every OTel
  metric — including the ones the framework's own instrumentation records — was dropped by the
  API's no-op provider.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import metrics, trace  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse, Response  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.observability.auto_configuration import (  # noqa: E402
    MeterProviderAutoConfiguration,
    TracingAutoConfiguration,
)
from pyfly.web.adapters.starlette.app import create_app  # noqa: E402
from pyfly.web.adapters.starlette.filters.tracing_filter import TracingFilter  # noqa: E402
from pyfly.web.adapters.starlette.management_app import create_management_app  # noqa: E402

if not isinstance(trace.get_tracer_provider(), TracerProvider):
    trace.set_tracer_provider(TracerProvider())
_exporter = InMemorySpanExporter()
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(_exporter))  # type: ignore[attr-defined]


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def _server_span_paths() -> list[str]:
    return [
        str(s.attributes.get("url.path")) for s in _exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER
    ]


class TestTracingFilterExcludes:
    async def test_the_actuator_is_excluded_by_default(self) -> None:
        _exporter.clear()

        async def call_next(_req: Request) -> Response:
            return PlainTextResponse("ok")

        f = TracingFilter()
        # The chain consults should_not_filter before do_filter; this is the decision it reads.
        assert f.should_not_filter(_request("/actuator/health"))
        assert f.should_not_filter(_request("/actuator"))
        assert not f.should_not_filter(_request("/orders"))
        for path in ("/actuator/health", "/orders"):
            if not f.should_not_filter(_request(path)):
                await f.do_filter(_request(path), call_next)
        assert _server_span_paths() == ["/orders"]

    def test_patterns_are_per_instance(self) -> None:
        custom = TracingFilter(exclude_patterns=["/internal/*"])
        assert custom.should_not_filter(_request("/internal/ping"))
        assert not custom.should_not_filter(_request("/actuator/health"))
        # The default instance is untouched: the patterns live on the instance, not the class.
        assert TracingFilter().should_not_filter(_request("/actuator/health"))

    def test_create_app_reads_the_configured_patterns(self) -> None:
        cfg = Config({"pyfly": {"observability": {"tracing": {"exclude-patterns": "/internal/*, /ready"}}}})
        ctx = ApplicationContext(cfg)
        app = create_app(context=ctx, docs_enabled=False)
        filters = _chain_filters(app)
        (tracing,) = [f for f in filters if isinstance(f, TracingFilter)]
        assert tracing.exclude_patterns == ["/internal/*", "/ready"]

    async def test_the_management_app_honours_the_same_setting(self) -> None:
        cfg = Config({"pyfly": {"observability": {"tracing": {"exclude-patterns": ["/actuator/*", "/admin/*"]}}}})
        ctx = ApplicationContext(cfg)
        await ctx.start()
        try:
            mgmt = create_management_app(
                ctx,
                health_agg=None,
                http_exchange_recorder=None,
                admin_trace_collector=None,
                actuator_active=True,
                admin_enabled=False,
                base_path="",
            )
            (tracing,) = [f for f in _chain_filters(mgmt) if isinstance(f, TracingFilter)]
            assert tracing.exclude_patterns == ["/actuator/*", "/admin/*"]
            _exporter.clear()
            assert TestClient(mgmt).get("/actuator/health").status_code == 200
            assert "/actuator/health" not in _server_span_paths()
        finally:
            await ctx.stop()


def _chain_filters(app: Any) -> list[Any]:
    from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware

    mws = [m for m in app.user_middleware if m.cls is WebFilterChainMiddleware]
    return list(mws[0].kwargs["filters"]) if mws else []


class TestMeterProvider:
    def test_metrics_endpoint_is_derived_from_the_traces_endpoint(self) -> None:
        derive = MeterProviderAutoConfiguration._otlp_metrics_endpoint
        assert derive("http://collector:4318/v1/traces") == "http://collector:4318/v1/metrics"
        assert derive("http://collector:4318") == "http://collector:4318/v1/metrics"
        assert derive("http://collector:4318/") == "http://collector:4318/v1/metrics"
        assert derive("http://collector:4318/custom/metrics") == "http://collector:4318/custom/metrics"

    def test_no_endpoint_means_a_provider_with_no_reader(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
        provider = MeterProviderAutoConfiguration().meter_provider(Config({}))
        assert isinstance(provider, MeterProvider)
        assert list(provider._sdk_config.metric_readers) == []
        # Instruments still work — a service records; nothing is exported.
        provider.get_meter("t").create_counter("requests").add(1)

    def test_an_endpoint_wires_an_otlp_http_reader(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
        cfg = Config(
            {"pyfly": {"observability": {"tracing": {"otlp": {"endpoint": "http://collector:4318/v1/traces"}}}}}
        )
        provider = MeterProviderAutoConfiguration().meter_provider(cfg)
        (reader,) = provider._sdk_config.metric_readers
        assert isinstance(reader, PeriodicExportingMetricReader)
        assert reader._exporter._endpoint == "http://collector:4318/v1/metrics"
        provider.shutdown()

    def test_the_metrics_endpoint_may_be_set_on_its_own(self) -> None:
        cfg = Config({"pyfly": {"observability": {"metrics": {"otlp": {"endpoint": "http://m:4318/v1/metrics"}}}}})
        provider = MeterProviderAutoConfiguration().meter_provider(cfg)
        (reader,) = provider._sdk_config.metric_readers
        assert reader._exporter._endpoint == "http://m:4318/v1/metrics"
        provider.shutdown()

    async def test_the_provider_is_a_bean_and_the_global_one(self) -> None:
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        try:
            provider = ctx.get_bean(MeterProvider)
            assert isinstance(provider, MeterProvider)
            assert metrics.get_meter_provider() is provider or isinstance(metrics.get_meter_provider(), MeterProvider)
            assert isinstance(ctx.get_bean(TracerProvider), TracerProvider)
        finally:
            await ctx.stop()

    def test_the_two_auto_configurations_share_the_service_name(self) -> None:
        cfg = Config({"pyfly": {"app": {"name": "orders"}}})
        provider = MeterProviderAutoConfiguration().meter_provider(cfg)
        assert provider._sdk_config.resource.attributes["service.name"] == "orders"
        assert TracingAutoConfiguration._service_name(cfg) == "orders"
