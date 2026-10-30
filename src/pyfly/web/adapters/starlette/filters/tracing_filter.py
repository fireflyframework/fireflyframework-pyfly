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
"""TracingFilter — extracts the inbound W3C trace context and opens a SERVER span.

So every span created during the request (e.g. via ``@span``) and every log line is part
of the upstream distributed trace. No-op when opentelemetry is not installed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, cast

from starlette.requests import Request
from starlette.responses import Response

from pyfly.container.exceptions import NoSuchBeanError
from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.observability.propagation import extract_context, has_otel
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover - exercised without the observability extra
    trace = None  # type: ignore[assignment]


#: Paths that get no SERVER span unless configured otherwise. Liveness and readiness probes hit
#: the actuator at 1 Hz per replica; traced, they outnumber every business span in the store
#: and say nothing anyone will ever look up by trace id.
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = ("/actuator", "/actuator/*")

#: The configuration key both ``create_app`` and ``create_management_app`` read: a list, or a
#: comma-separated string, of ``fnmatch`` globs.
EXCLUDE_PATTERNS_KEY = "pyfly.observability.tracing.exclude-patterns"


@order(HIGHEST_PRECEDENCE + 60)  # just after CorrelationFilter (+50)
class TracingFilter(OncePerRequestFilter):
    """Opens a server span as a child of the extracted upstream trace context.

    ``exclude_patterns`` are held on the INSTANCE. The base class declares them as a class
    attribute, and since ``create_app`` builds its own ``TracingFilter()`` while assembling the
    chain, the only way an application could exclude a path was to mutate the class before the
    app was built — a boot-order trick every service reinvented. The constructor and
    :meth:`from_config` are the supported doors now.
    """

    def __init__(
        self,
        exclude_patterns: Sequence[str] | None = None,
        *,
        tracer_provider: trace.TracerProvider | None = None,
        tracer_provider_resolver: Callable[[], trace.TracerProvider | None] | None = None,
    ) -> None:
        if tracer_provider is not None and tracer_provider_resolver is not None:
            raise ValueError("Supply either a tracer provider or a provider resolver")
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS if exclude_patterns is None else exclude_patterns)
        self._tracer_provider = tracer_provider
        self._tracer_provider_resolver = tracer_provider_resolver

    @classmethod
    def from_config(
        cls,
        config: object | None,
        *,
        tracer_provider: trace.TracerProvider | None = None,
        tracer_provider_resolver: Callable[[], trace.TracerProvider | None] | None = None,
    ) -> TracingFilter:
        """Build the filter from ``pyfly.observability.tracing.exclude-patterns``.

        Accepts a YAML list or a comma-separated string; an absent key keeps the default.
        """
        raw = getattr(config, "get")(EXCLUDE_PATTERNS_KEY) if config is not None else None  # noqa: B009
        patterns: list[str] | None
        if raw is None or raw == "":
            patterns = None
        elif isinstance(raw, str):
            patterns = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            patterns = [str(p).strip() for p in raw if str(p).strip()]
        return cls(
            exclude_patterns=patterns,
            tracer_provider=tracer_provider,
            tracer_provider_resolver=tracer_provider_resolver,
        )

    @classmethod
    def from_context(cls, context: ApplicationContext | None) -> TracingFilter:
        """Resolve the application's provider per request, after boot and across restarts.

        The filter borrows the provider. Context-free hosts and contexts without a tracing
        bean retain the OpenTelemetry global-provider behavior.
        """
        if context is None:
            return cls()

        def resolve() -> trace.TracerProvider | None:
            if trace is None:
                return None
            try:
                return context.get_bean(trace.TracerProvider)  # type: ignore[type-abstract]
            except NoSuchBeanError:
                return None

        return cls.from_config(context.config, tracer_provider_resolver=resolve)

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        if not has_otel():
            return cast(Response, await call_next(request))
        parent = extract_context(request.headers)
        provider = self._tracer_provider
        if self._tracer_provider_resolver is not None:
            provider = self._tracer_provider_resolver()
        tracer = provider.get_tracer("pyfly") if provider is not None else trace.get_tracer("pyfly")
        with tracer.start_as_current_span(
            f"{request.method} {request.url.path}",
            context=parent,
            kind=trace.SpanKind.SERVER,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                response = cast(Response, await call_next(request))
            except BaseException as error:
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(trace.StatusCode.ERROR)
                raise
            try:
                span.set_attribute("http.request.method", request.method)
                span.set_attribute("url.path", request.url.path)
                span.set_attribute("http.response.status_code", response.status_code)
            except Exception:  # noqa: BLE001 - attribute tagging must never break the response
                pass
            return response
