"""Issue #151: exported traces and metrics retain the platform's service identity."""

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from pyfly.core.config import Config
from pyfly.observability.auto_configuration import MeterProviderAutoConfiguration, TracingAutoConfiguration


@pytest.fixture
def otlp_receiver(monkeypatch):
    for name in list(os.environ):
        if name.startswith("OTEL_") or name in {
            "PYFLY_APP_NAME",
            "PYFLY_OBSERVABILITY_TRACING_SERVICE_NAME",
            "PYFLY_OBSERVABILITY_TRACING_EXPORTER",
            "PYFLY_OBSERVABILITY_TRACING_OTLP_ENDPOINT",
            "PYFLY_OBSERVABILITY_METRICS_OTLP_ENDPOINT",
            "PYFLY_OBSERVABILITY_TRACING_REGISTER_GLOBAL",
            "PYFLY_OBSERVABILITY_METRICS_REGISTER_GLOBAL",
        }:
            monkeypatch.delenv(name)
    received = queue.Queue()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.put((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "app_name,explicit_name,env,expected",
    [
        (None, None, {"OTEL_SERVICE_NAME": "core-flydocs"}, "core-flydocs"),
        (
            None,
            None,
            {"OTEL_RESOURCE_ATTRIBUTES": "service.name=core-flydocs-worker"},
            "core-flydocs-worker",
        ),
        (
            "shared-app",
            None,
            {"OTEL_SERVICE_NAME": "core-flydocs-bbox-worker", "OTEL_RESOURCE_ATTRIBUTES": "service.name=ignored"},
            "core-flydocs-bbox-worker",
        ),
        (
            None,
            None,
            {"OTEL_SERVICE_NAME": "", "OTEL_RESOURCE_ATTRIBUTES": "service.name=attributes-worker"},
            "attributes-worker",
        ),
        ("app-fallback", None, {"OTEL_SERVICE_NAME": "", "OTEL_RESOURCE_ATTRIBUTES": ""}, "app-fallback"),
        (None, None, {}, "pyfly-app"),
        (
            "shared-app",
            "explicit-service",
            {"OTEL_SERVICE_NAME": "injected-service", "OTEL_RESOURCE_ATTRIBUTES": "service.name=attributes-service"},
            "explicit-service",
        ),
        (
            None,
            "yaml-service",
            {"PYFLY_OBSERVABILITY_TRACING_SERVICE_NAME": "pyfly-env-service", "OTEL_SERVICE_NAME": "otel-service"},
            "pyfly-env-service",
        ),
        ("shared-app", "", {"OTEL_SERVICE_NAME": "injected-service"}, "injected-service"),
        (
            None,
            None,
            {"OTEL_RESOURCE_ATTRIBUTES": "invalid,service.name=encoded%2Cworker%3Dblue"},
            "encoded,worker=blue",
        ),
        ("app-fallback", None, {"OTEL_RESOURCE_ATTRIBUTES": "service.name="}, "app-fallback"),
    ],
    ids=[
        "standard-service-env",
        "resource-attributes",
        "service-env-precedence",
        "empty-env-uses-attributes",
        "empty-env-uses-app",
        "framework-fallback",
        "explicit-pyfly-config",
        "pyfly-env-override",
        "empty-explicit-config",
        "sdk-resource-parsing",
        "empty-attribute-uses-app",
    ],
)
def test_exported_signals_share_resolved_service_identity(
    tmp_path: Path, monkeypatch, otlp_receiver, app_name, explicit_name, env, expected
):
    endpoint, received = otlp_receiver
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    resource_attributes = "service.namespace=platform,deployment.environment.name=test"
    if env.get("OTEL_RESOURCE_ATTRIBUTES"):
        resource_attributes += "," + env["OTEL_RESOURCE_ATTRIBUTES"]
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", resource_attributes)
    tracing = {"register-global": False, "otlp": {"endpoint": endpoint}}
    if explicit_name is not None:
        tracing["service-name"] = explicit_name
    data = {"pyfly": {"observability": {"tracing": tracing, "metrics": {"register-global": False}}}}
    if app_name is not None:
        data["pyfly"]["app"] = {"name": app_name}
    (tmp_path / "pyfly.yaml").write_text(json.dumps(data))
    config = Config.from_sources(tmp_path)
    tracer = TracingAutoConfiguration().tracer_provider(config)
    meter = None
    try:
        meter = MeterProviderAutoConfiguration().meter_provider(config)
        with tracer.get_tracer("issue-151").start_as_current_span("workload-operation"):
            meter.get_meter("issue-151").create_counter("workload.operations").add(1)
        assert tracer.force_flush(timeout_millis=5000)
        assert meter.force_flush(timeout_millis=5000)
        exports = dict(received.get(timeout=5) for _ in range(2))
        traces = ExportTraceServiceRequest.FromString(exports["/v1/traces"])
        metrics = ExportMetricsServiceRequest.FromString(exports["/v1/metrics"])
        assert traces.resource_spans[0].scope_spans[0].spans[0].name == "workload-operation"
        assert metrics.resource_metrics[0].scope_metrics[0].metrics[0].sum.data_points[0].as_int == 1
        for resource in (traces.resource_spans[0].resource, metrics.resource_metrics[0].resource):
            attributes = {item.key: item.value.string_value for item in resource.attributes}
            assert attributes["service.name"] == expected
            assert attributes["service.namespace"] == "platform"
            assert attributes["deployment.environment.name"] == "test"
    finally:
        tracer.shutdown()
        if meter is not None:
            meter.shutdown()
