# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Web starter — web-tier microservice bundle.

Activates the HTTP/REST tier: web framework adapter (Starlette or FastAPI),
ASGI server (Granian/Uvicorn/Hypercorn), validation, request filters,
actuator, observability, and the security-headers / CORS / OAuth2 filter
chain. The Java side bundles these inside ``starter-core`` (because
WebFlux is always present in JVM stacks); .NET likewise rolls them into
``Starter.Core``. In Python we keep them split so a non-HTTP service
(worker, scheduler, CLI tool) can opt out of the web stack entirely.

Use ``@enable_web_stack`` for a service that primarily exposes a REST
API. Combine with ``@enable_core_stack`` (or higher) when you also need
CQRS, EDA, cache, etc.

Usage — declarative::

    from pyfly.core import pyfly_application
    from pyfly.starters.web import enable_web_stack

    @enable_web_stack
    @pyfly_application(name="my-api", scan_packages=["my_api"])
    class Application:
        pass

Usage — imperative (parity with .NET ``services.AddFireflyWeb``)::

    from pyfly.starters.web import register_web_stack

    app = PyFlyApplication(MyApp)
    register_web_stack(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-exports — single import line for a controller-tier file.
from pyfly.container import controller, rest_controller
from pyfly.web import (
    Body,
    Cookie,
    File,
    Header,
    PathVar,
    QueryParam,
    UploadedFile,
    Valid,
    controller_advice,
    delete_mapping,
    exception_handler,
    get_mapping,
    patch_mapping,
    post_mapping,
    put_mapping,
    request_mapping,
    sse_mapping,
)

if TYPE_CHECKING:
    from pyfly.core.application import PyFlyApplication

#: Property keys this starter activates at boot.
WEB_STACK_PROPERTIES: dict[str, str] = {
    # Web framework — Starlette is the default; FastAPI adapter binds if installed.
    "pyfly.web.enabled": "true",
    # ASGI server — Granian / Uvicorn / Hypercorn auto-detected.
    "pyfly.server.enabled": "true",
    # Observability — metrics + tracing.
    "pyfly.observability.enabled": "true",
    "pyfly.metrics.enabled": "true",
    "pyfly.tracing.enabled": "true",
    # Actuator — /health, /info, /metrics, /prometheus
    "pyfly.actuator.enabled": "true",
    "pyfly.actuator.metrics.enabled": "true",
    # Resilience filters in front of every endpoint
    "pyfly.resilience.enabled": "true",
}


def enable_web_stack(cls: type) -> type:
    """Mark *cls* as a web-tier application.

    Setting ``__pyfly_starter_web__`` causes :class:`PyFlyApplication`
    to merge :data:`WEB_STACK_PROPERTIES` into the active config before
    auto-configurations run.
    """
    cls.__pyfly_starter_web__ = WEB_STACK_PROPERTIES  # type: ignore[attr-defined]
    return cls


def register_web_stack(app: PyFlyApplication) -> PyFlyApplication:
    """Imperative API — parity with .NET ``services.AddFireflyWeb``."""
    from pyfly.core.config import Config

    defaults: dict[str, object] = {}
    for dotted_key, raw_value in WEB_STACK_PROPERTIES.items():
        _merge_dotted(defaults, dotted_key, raw_value)
    app.config._data = Config._deep_merge(app.config._data, defaults)
    return app


def _merge_dotted(target: dict[str, object], key: str, value: object) -> None:
    parts = key.split(".")
    cursor: dict[str, object] = target
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[parts[-1]] = value


__all__ = [
    "Body",
    "Cookie",
    "File",
    "Header",
    "PathVar",
    "QueryParam",
    "UploadedFile",
    "Valid",
    "WEB_STACK_PROPERTIES",
    "controller",
    "controller_advice",
    "delete_mapping",
    "enable_web_stack",
    "exception_handler",
    "get_mapping",
    "patch_mapping",
    "post_mapping",
    "put_mapping",
    "register_web_stack",
    "request_mapping",
    "rest_controller",
    "sse_mapping",
]
