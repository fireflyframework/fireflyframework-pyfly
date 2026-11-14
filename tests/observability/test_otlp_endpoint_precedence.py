# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Endpoint precedence reaches the real HTTP exporter without rewriting signal URLs."""

import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pyfly.core.config import Config
from pyfly.observability.auto_configuration import MeterProviderAutoConfiguration, TracingAutoConfiguration


@pytest.fixture
def receiver(monkeypatch):
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)
    received = queue.Queue()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.put((self.path, body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("signal", ["tracing", "metrics"])
@pytest.mark.parametrize(
    ("explicit", "specific", "general", "expected"),
    [
        (None, "/custom", None, "/custom"),
        (None, "/custom", "", "/custom"),
        (None, "", "", "/"),
        ("/explicit", "/custom", "", "/explicit"),
        (None, None, "", "/v1/{signal}"),
        (None, None, "/legacy", "/legacy"),
    ],
)
def test_otlp_endpoint_precedence_delivers_to_selected_url(
    receiver, monkeypatch, signal, explicit, specific, general, expected
):
    base, received = receiver
    wire_signal = "traces" if signal == "tracing" else "metrics"
    if general is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", base + general)
    if specific is not None:
        monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{wire_signal.upper()}_ENDPOINT", base + specific)
    options = {"register-global": False}
    if explicit is not None:
        options["otlp"] = {"endpoint": base + explicit}
    config = Config({"pyfly": {"observability": {signal: options}}})
    if signal == "tracing":
        provider = TracingAutoConfiguration().tracer_provider(config)
    else:
        provider = MeterProviderAutoConfiguration().meter_provider(config)
    try:
        if signal == "tracing":
            with provider.get_tracer("endpoint-test").start_as_current_span("exported"):
                pass
        else:
            provider.get_meter("endpoint-test").create_counter("exported").add(1)
        assert provider.force_flush(timeout_millis=2000)
        assert not received.empty(), "Configured endpoint did not receive telemetry"
        path, body = received.get_nowait()
        assert path == expected.format(signal=wire_signal)
        assert body
    finally:
        provider.shutdown()


@pytest.mark.parametrize("signal", ["tracing", "metrics"])
def test_exporter_none_overrides_signal_endpoint(receiver, monkeypatch, signal):
    base, received = receiver
    wire_signal = "traces" if signal == "tracing" else "metrics"
    monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{wire_signal.upper()}_ENDPOINT", base + "/custom")
    config = Config({"pyfly": {"observability": {signal: {"register-global": False, "exporter": "none"}}}})
    if signal == "tracing":
        provider = TracingAutoConfiguration().tracer_provider(config)
    else:
        provider = MeterProviderAutoConfiguration().meter_provider(config)
    try:
        if signal == "tracing":
            with provider.get_tracer("endpoint-test").start_as_current_span("disabled"):
                pass
        else:
            provider.get_meter("endpoint-test").create_counter("disabled").add(1)
        assert provider.force_flush(timeout_millis=2000)
    finally:
        provider.shutdown()
    assert received.empty()
