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
"""Observability auto-configuration — metrics registry and tracer provider beans."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

import logging
import os
from typing import Any

try:
    from pyfly.observability.metrics import MetricsRegistry
except ImportError:
    MetricsRegistry = object  # type: ignore[misc,assignment]

try:
    from opentelemetry.sdk.trace import TracerProvider
except ImportError:
    TracerProvider = object  # type: ignore[misc,assignment]

try:
    from opentelemetry.sdk.metrics import MeterProvider
except ImportError:
    MeterProvider = object  # type: ignore[misc,assignment]

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_class
from pyfly.core.config import Config

_logger = logging.getLogger(__name__)


@auto_configuration
@conditional_on_class("prometheus_client")
class MetricsAutoConfiguration:
    """Auto-configures a MetricsRegistry bean when prometheus_client is installed."""

    @bean
    def metrics_registry(self) -> MetricsRegistry:
        return MetricsRegistry()

    # NOTE: The HTTP ``MetricsFilter`` is NOT registered as a bean. It must join
    # the WebFilter chain while the ASGI app is being assembled in ``create_app``
    # — which runs before ``ApplicationContext.start()`` instantiates beans — so
    # ``create_app`` owns the instance directly (gated on
    # ``pyfly.observability.metrics.enabled``). A bean here would be built too
    # late to ever reach the chain.


@auto_configuration
@conditional_on_class("opentelemetry")
class TracingAutoConfiguration:
    """Auto-configures an OpenTelemetry TracerProvider when opentelemetry is installed."""

    @staticmethod
    def _service_name(config: Config) -> str:
        """``pyfly.observability.tracing.service-name``, else ``pyfly.app.name``, else ``pyfly-app``.

        Shared with :class:`MeterProviderAutoConfiguration` so traces and metrics carry one
        ``service.name`` — a dashboard joins the two on it.
        """
        return str(
            config.get(
                "pyfly.observability.tracing.service-name",
                config.get("pyfly.app.name", "pyfly-app"),
            )
        )

    @bean
    def tracer_provider(self, config: Config) -> TracerProvider:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider as _TracerProvider

        service_name = self._service_name(config)
        resource = Resource.create({"service.name": service_name})
        provider = _TracerProvider(resource=resource)
        # Attach a span processor + exporter (audit #153). Without one, every
        # @span span is recorded into the provider and immediately discarded.
        self._install_span_processor(provider, config)
        trace.set_tracer_provider(provider)
        return provider

    _OTLP_TRACES_PATH = "/v1/traces"

    @classmethod
    def _otlp_traces_endpoint(cls, configured: str) -> str:
        """Turn a configured OTLP endpoint into the full traces URL the exporter wants.

        The OpenTelemetry specification draws a line this method restores. ``OTEL_EXPORTER_OTLP_ENDPOINT``
        is a BASE url — the SDK appends the per-signal path to it — while ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``
        and the exporter's own ``endpoint=`` argument are the COMPLETE url, used verbatim. Reading the base
        variable and passing it straight to ``OTLPSpanExporter(endpoint=...)`` collapsed the two: an operator
        who set the spec-correct ``http://collector:4318`` got an exporter POSTing to ``http://collector:4318``,
        which is not a signal endpoint, and every span was dropped with nothing logged. The only way to make
        it work was to write a value into the base variable that the spec says is not a base.

        Both spellings work now. A url whose path is empty (or bare ``/``) is treated as a base and gains
        ``/v1/traces``; anything with a path is taken as already complete and returned untouched.
        """
        from urllib.parse import urlparse

        parsed = urlparse(configured)

        if parsed.path in ("", "/"):
            return configured.rstrip("/") + cls._OTLP_TRACES_PATH

        return configured

    @staticmethod
    def _install_span_processor(provider: Any, config: Config) -> None:
        """Wire a BatchSpanProcessor + exporter chosen from configuration.

        ``pyfly.observability.tracing.exporter`` selects ``otlp`` | ``console`` |
        ``none``. When unset, OTLP is used iff an endpoint is configured (via
        ``pyfly.observability.tracing.otlp.endpoint`` or the standard
        ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var); otherwise no exporter is wired
        and a single info line is logged so the drop is not silent.
        """
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        otlp_endpoint = config.get("pyfly.observability.tracing.otlp.endpoint") or os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT"
        )
        kind = str(config.get("pyfly.observability.tracing.exporter", "")).strip().lower()
        if not kind:
            kind = "otlp" if otlp_endpoint else "none"

        if kind == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            return

        if kind == "otlp":
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found, unused-ignore]
                    OTLPSpanExporter,
                )
            except ImportError:
                _logger.warning(
                    "Tracing exporter 'otlp' requested but opentelemetry-exporter-otlp is not "
                    "installed — spans will be dropped. Install it or set "
                    "pyfly.observability.tracing.exporter=console."
                )
                return
            exporter = (
                OTLPSpanExporter(endpoint=TracingAutoConfiguration._otlp_traces_endpoint(otlp_endpoint))
                if otlp_endpoint
                else OTLPSpanExporter()
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            return

        _logger.info(
            "Tracing is active but no span exporter is configured — spans are dropped. "
            "Set pyfly.observability.tracing.exporter=otlp|console or OTEL_EXPORTER_OTLP_ENDPOINT."
        )


@auto_configuration
@conditional_on_class("opentelemetry.sdk.metrics")
class MeterProviderAutoConfiguration:
    """Auto-configures an OpenTelemetry ``MeterProvider`` beside the ``TracerProvider``.

    Until 26.09.05 the framework set a tracer provider and no meter provider, so every OTel
    metric an application (or the framework's own instrumentation) recorded went to the API's
    no-op provider and was dropped, silently. The reader is OTLP/HTTP, to the metrics endpoint
    derived from the traces one (``/v1/traces`` -> ``/v1/metrics``) unless
    ``pyfly.observability.metrics.otlp.endpoint`` or ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`` names
    it. With no endpoint at all the provider has no reader: instruments still work, nothing is
    exported, and tests stay offline.
    """

    _OTLP_METRICS_PATH = "/v1/metrics"
    _OTLP_TRACES_PATH = "/v1/traces"

    @classmethod
    def _otlp_metrics_endpoint(cls, configured: str) -> str:
        """Turn a traces or base OTLP url into the metrics url.

        Same line the traces resolver draws: a url with no path is a base and gains
        ``/v1/metrics``; a url ending in ``/v1/traces`` is the sibling signal and is rewritten;
        anything else is taken as the complete metrics url.
        """
        from urllib.parse import urlparse

        parsed = urlparse(configured)
        if parsed.path in ("", "/"):
            return configured.rstrip("/") + cls._OTLP_METRICS_PATH
        trimmed = configured.rstrip("/")
        if trimmed.endswith(cls._OTLP_TRACES_PATH):
            return trimmed[: -len(cls._OTLP_TRACES_PATH)] + cls._OTLP_METRICS_PATH
        return configured

    @bean
    def meter_provider(self, config: Config) -> MeterProvider:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider as _MeterProvider
        from opentelemetry.sdk.resources import Resource

        explicit = config.get("pyfly.observability.metrics.otlp.endpoint") or os.environ.get(
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"
        )
        derived_from = config.get("pyfly.observability.tracing.otlp.endpoint") or os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT"
        )
        endpoint = (
            str(explicit) if explicit else (self._otlp_metrics_endpoint(str(derived_from)) if derived_from else "")
        )

        readers: list[Any] = []
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # type: ignore[import-not-found, unused-ignore]
                    OTLPMetricExporter,
                )
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            except ImportError:
                _logger.warning(
                    "An OTLP metrics endpoint is configured but opentelemetry-exporter-otlp is not "
                    "installed — metrics will be dropped. Install it or unset the endpoint."
                )
            else:
                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint)))
        else:
            _logger.info(
                "Metrics are active but no OTLP endpoint is configured — instruments record, nothing is exported. "
                "Set pyfly.observability.metrics.otlp.endpoint or OTEL_EXPORTER_OTLP_ENDPOINT."
            )

        provider = _MeterProvider(
            resource=Resource.create({"service.name": TracingAutoConfiguration._service_name(config)}),
            metric_readers=readers,
        )
        metrics.set_meter_provider(provider)
        return provider
