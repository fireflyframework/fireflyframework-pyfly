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
"""Shared actuator wiring used by both the Starlette and FastAPI adapters.

Keeps the two web adapters from drifting: one place registers every built-in
endpoint, applies the Spring-style exposure model, and builds the routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.actuator.health import HealthAggregator
    from pyfly.actuator.http_exchanges import HttpExchangeRecorder
    from pyfly.context.application_context import ApplicationContext


def resolve_actuator_active(context: ApplicationContext | None, actuator_enabled: bool | None) -> bool:
    """Resolve whether the actuator is active (tri-state ``actuator_enabled``).

    ``None`` -> derive from config (Spring default: ON when a context is present),
    via ``pyfly.management.enabled`` (legacy alias ``pyfly.web.actuator.enabled``).
    """
    if actuator_enabled is not None:
        return actuator_enabled
    if context is None:
        return False
    return str(
        context.config.get(
            "pyfly.management.enabled",
            context.config.get("pyfly.web.actuator.enabled", "true"),
        )
    ).lower() in ("true", "1", "yes")


def _health_show(config: Any, key: str) -> bool:
    """Map ``pyfly.management.endpoint.health.{key}`` (never|when-authorized|always)
    to a boolean. Defaults to ``when-authorized`` (shown)."""
    if config is None:
        return True
    return str(config.get(f"pyfly.management.endpoint.health.{key}", "when-authorized")).lower() != "never"


def build_actuator_routes(
    context: ApplicationContext | None,
    health_aggregator: HealthAggregator,
    http_recorder: HttpExchangeRecorder | None = None,
) -> list[Any]:
    """Register all built-in actuator endpoints and return the exposed routes.

    Returns a list of Starlette ``Route`` objects (typed ``Any`` to keep this
    module free of a vendor import — Starlette lives only in the adapters)."""
    from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
    from pyfly.actuator.endpoints.beans_endpoint import BeansEndpoint
    from pyfly.actuator.endpoints.caches_endpoint import CachesEndpoint
    from pyfly.actuator.endpoints.conditions_endpoint import ConditionsEndpoint
    from pyfly.actuator.endpoints.configprops_endpoint import ConfigPropsEndpoint
    from pyfly.actuator.endpoints.env_endpoint import EnvEndpoint
    from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint
    from pyfly.actuator.endpoints.info_endpoint import InfoEndpoint
    from pyfly.actuator.endpoints.loggers_endpoint import LoggersEndpoint
    from pyfly.actuator.endpoints.mappings_endpoint import MappingsEndpoint
    from pyfly.actuator.endpoints.metrics_endpoint import MetricsEndpoint
    from pyfly.actuator.endpoints.refresh_endpoint import RefreshEndpoint
    from pyfly.actuator.endpoints.scheduledtasks_endpoint import ScheduledTasksEndpoint
    from pyfly.actuator.endpoints.threaddump_endpoint import ThreadDumpEndpoint
    from pyfly.actuator.exposure import base_path, is_web_exposed, web_exposure
    from pyfly.actuator.registry import ActuatorRegistry

    config = context.config if context is not None else None
    registry = ActuatorRegistry(config=config)

    # Context-free endpoints. Health honors show-details / show-components
    # (never -> hide; when-authorized/always -> show, since the actuator has no
    # dedicated auth layer, "when-authorized" is treated as authorized).
    registry.register(
        HealthEndpoint(
            health_aggregator,
            show_details=_health_show(config, "show-details"),
            show_components=_health_show(config, "show-components"),
        )
    )
    registry.register(LoggersEndpoint())
    registry.register(MetricsEndpoint())
    registry.register(ThreadDumpEndpoint())

    # Context-backed endpoints.
    if context is not None:
        registry.register(BeansEndpoint(context))
        registry.register(EnvEndpoint(context))
        registry.register(InfoEndpoint(context))
        registry.register(ConfigPropsEndpoint(context))
        registry.register(MappingsEndpoint(context))
        registry.register(ScheduledTasksEndpoint(context))
        registry.register(ConditionsEndpoint(context))
        registry.register(CachesEndpoint(context))
        registry.register(RefreshEndpoint(context))

    # Prometheus scrape endpoint (only when prometheus_client is installed).
    try:
        import prometheus_client  # noqa: F401

        from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint

        registry.register(PrometheusEndpoint())
    except ImportError:
        pass

    # HTTP exchanges (only when a recorder filter is wired into the chain).
    if http_recorder is not None:
        from pyfly.actuator.endpoints.httpexchanges_endpoint import HttpExchangesEndpoint

        registry.register(HttpExchangesEndpoint(http_recorder))

    # Custom user ActuatorEndpoint beans.
    if context is not None:
        registry.discover_from_context(context)

    # Spring Boot secure-by-default web exposure (only health + info unless opted in).
    include, exclude = web_exposure(config)
    exposed = {eid for eid in registry.get_enabled_endpoints() if is_web_exposed(eid, include, exclude)}
    return make_starlette_actuator_routes(registry, exposed_ids=exposed, base_path=base_path(config))


def make_http_exchange_filter(
    context: ApplicationContext | None, actuator_active: bool
) -> tuple[Any | None, Any | None]:
    """Create the (recorder, filter) pair for ``/actuator/httpexchanges`` when active."""
    if not actuator_active or context is None:
        return None, None
    from pyfly.actuator.http_exchanges import HttpExchangeRecorder, HttpExchangeRecorderFilter

    recorder = HttpExchangeRecorder()
    return recorder, HttpExchangeRecorderFilter(recorder)
