"""Application overrides and process/context OpenTelemetry ownership."""

import subprocess
import sys

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pyfly.container import bean, configuration, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.observability.auto_configuration import MeterProviderAutoConfiguration, TracingAutoConfiguration


async def test_user_api_provider_beans_override_defaults_without_changing_globals():
    tracer = TracerProvider(shutdown_on_exit=False)
    meter = MeterProvider(shutdown_on_exit=False)
    before = (trace.get_tracer_provider(), metrics.get_meter_provider())

    @configuration
    class Providers:
        @bean(destroy_method="shutdown")
        def traces(self) -> trace.TracerProvider:
            return tracer

        @bean(destroy_method="shutdown")
        def meters(self) -> metrics.MeterProvider:
            return meter

    @service
    class InstrumentedService:
        def __init__(self, traces: trace.TracerProvider, meters: metrics.MeterProvider):
            self.traces = traces
            self.meters = meters

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Providers)
    ctx.register_bean(InstrumentedService)
    await ctx.start()
    try:
        svc = ctx.get_bean(InstrumentedService)
        assert svc.traces is tracer and svc.meters is meter
        assert ctx.get_bean(TracerProvider) is tracer
        assert ctx.get_bean(MeterProvider) is meter
        assert (trace.get_tracer_provider(), metrics.get_meter_provider()) == before
    finally:
        await ctx.stop()


async def test_context_local_providers_are_closed_and_rebuilt_without_global_mutation(monkeypatch):
    monkeypatch.setattr(
        "pyfly.config.auto.discover_auto_configurations",
        lambda: [TracingAutoConfiguration, MeterProviderAutoConfiguration],
    )
    before = (trace.get_tracer_provider(), metrics.get_meter_provider())
    ctx = ApplicationContext(
        Config(
            {"pyfly": {"observability": {"tracing": {"register-global": False}, "metrics": {"register-global": False}}}}
        )
    )
    await ctx.start()
    tracer = ctx.get_bean(TracerProvider)
    meter = ctx.get_bean(MeterProvider)
    exporter = InMemorySpanExporter()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    with tracer.get_tracer("test").start_as_current_span("before-stop"):
        pass
    assert len(exporter.get_finished_spans()) == 1
    await ctx.stop()
    assert exporter._stopped
    assert meter._shutdown
    assert (trace.get_tracer_provider(), metrics.get_meter_provider()) == before
    await ctx.start()
    assert ctx.get_bean(TracerProvider) is not tracer
    assert ctx.get_bean(MeterProvider) is not meter
    await ctx.stop()


def test_default_globals_are_reused_across_contexts_without_extra_resources():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.observability.tracing import span

async def main():
    first = ApplicationContext(Config({}))
    await first.start()
    tracer, meter = trace.get_tracer_provider(), metrics.get_meter_provider()
    exporter = InMemorySpanExporter()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    await first.stop()
    second = ApplicationContext(Config({}))
    await second.start()
    assert second.get_bean(type(tracer)) is tracer
    assert second.get_bean(type(meter)) is meter
    @span("after-restart")
    def work():
        pass
    work()
    assert len(exporter.get_finished_spans()) == 1
    await second.stop()
    assert not exporter._stopped
    tracer.shutdown()
    meter.shutdown()
asyncio.run(main())
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Overriding of current" not in result.stderr


async def test_meter_provider_construction_failure_releases_started_reader(monkeypatch):
    import pytest
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    readers = []

    def reader(*args, **kwargs):
        result = PeriodicExportingMetricReader(*args, **kwargs)
        readers.append(result)
        return result

    def failing_provider(*args, **kwargs):
        raise RuntimeError("provider construction failed")

    monkeypatch.setattr("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader", reader)
    monkeypatch.setattr("opentelemetry.sdk.metrics.MeterProvider", failing_provider)
    auto_config = MeterProviderAutoConfiguration()
    config = Config(
        {
            "pyfly": {
                "observability": {
                    "metrics": {
                        "register-global": False,
                        "otlp": {"endpoint": "http://127.0.0.1:1/v1/metrics"},
                    }
                }
            }
        }
    )
    try:
        with pytest.raises(RuntimeError, match="provider construction failed"):
            auto_config.meter_provider(config)
        assert readers[0]._daemon_thread.is_alive()
        await auto_config.close()
        assert not readers[0]._daemon_thread.is_alive()
        await auto_config.close()
    finally:
        if readers and readers[0]._daemon_thread.is_alive():
            readers[0].shutdown()


async def test_metric_reader_construction_failure_releases_exporter(monkeypatch):
    import pytest
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    exporters = []

    def exporter(*args, **kwargs):
        result = OTLPMetricExporter(*args, **kwargs)
        exporters.append(result)
        return result

    def failing_reader(*args, **kwargs):
        raise RuntimeError("reader construction failed")

    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter", exporter)
    monkeypatch.setattr("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader", failing_reader)
    auto_config = MeterProviderAutoConfiguration()
    config = Config(
        {
            "pyfly": {
                "observability": {
                    "metrics": {
                        "register-global": False,
                        "otlp": {"endpoint": "http://127.0.0.1:1/v1/metrics"},
                    }
                }
            }
        }
    )
    try:
        with pytest.raises(RuntimeError, match="reader construction failed"):
            auto_config.meter_provider(config)
        await auto_config.close()
        assert exporters[0]._shutdown
        await auto_config.close()
    finally:
        if exporters and not exporters[0]._shutdown:
            exporters[0].shutdown()
