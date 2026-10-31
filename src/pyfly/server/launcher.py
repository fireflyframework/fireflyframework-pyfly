# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public synchronous launch boundary, before the application's async lifecycle."""

from __future__ import annotations

import importlib
import importlib.util
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from pyfly.config.properties.server import ServerProperties
from pyfly.server.ports.event_loop import EventLoopPort
from pyfly.server.ports.outbound import ApplicationServerPort


def select_server(kind: str = "auto") -> ApplicationServerPort:
    """Select an installed native adapter; an explicit missing adapter fails."""
    choices = (
        ("granian", "GranianServerAdapter"),
        ("uvicorn", "UvicornServerAdapter"),
        ("hypercorn", "HypercornServerAdapter"),
    )
    if kind not in {"auto", *(name for name, _ in choices)}:
        raise ValueError(f"Unsupported application server type: {kind}")
    for name, adapter in choices:
        if kind in {"auto", name} and importlib.util.find_spec(name) is not None:
            module = importlib.import_module(f"pyfly.server.adapters.{name}.adapter")
            return cast(ApplicationServerPort, getattr(module, adapter)())
    raise ValueError(f"The requested application server is not installed: {kind}")


def select_event_loop(kind: str = "auto") -> EventLoopPort:
    """Select a native policy adapter without installing or opening a loop."""
    choices = (("uvloop", "UvloopEventLoopAdapter"), ("winloop", "WinloopEventLoopAdapter"))
    if kind not in {"auto", "asyncio", *(name for name, _ in choices)}:
        raise ValueError(f"Unsupported event loop type: {kind}")
    for name, adapter in choices:
        if kind in {"auto", name} and importlib.util.find_spec(name) is not None:
            module = importlib.import_module(f"pyfly.server.adapters.event_loop.{name}_adapter")
            return cast(EventLoopPort, getattr(module, adapter)())
    if kind in {"auto", "asyncio"}:
        from pyfly.server.adapters.event_loop.asyncio_adapter import AsyncioEventLoopAdapter

        return AsyncioEventLoopAdapter()
    raise ValueError(f"The requested event loop is not installed: {kind}")


def validate_server_properties(properties: ServerProperties) -> None:
    """Reject invalid listener/TLS input before changing policy or serving."""
    if (
        not isinstance(properties.host, str)
        or not properties.host
        or any(char in properties.host for char in ("\0", "\r", "\n"))
    ):
        raise ValueError("Configure a valid application listener host")
    if type(properties.port) is not int or not 0 <= properties.port <= 65535:
        raise ValueError("Application listener port must be an integer from 0 to 65535")
    if bool(properties.ssl_certfile) != bool(properties.ssl_keyfile):
        raise ValueError("HTTPS requires both certificate and key paths")
    from pyfly.server.tls import client_verify_mode

    client_verify_mode(properties)
    for path in (properties.ssl_certfile, properties.ssl_keyfile, properties.ssl_ca_certs):
        if path is None:
            continue
        try:
            valid = (
                isinstance(path, str)
                and bool(path)
                and not any(char in path for char in ("\0", "\r", "\n"))
                and Path(path).is_file()
            )
        except OSError:
            valid = False
        if not valid:
            raise ValueError("TLS certificate and key files must exist") from None


def launch(
    app: str | Any,
    properties: ServerProperties,
    *,
    server: ApplicationServerPort | None = None,
    event_loop: EventLoopPort | None = None,
) -> None:
    """Install the selected loop policy, then serve with its concrete loop name.

    Explicit ports belong to the caller's composition; defaults use the native
    installed-adapter policy. No application is imported, no context is created
    or started, and async application beans are not implicitly selected here.
    The server owns its normal ASGI application startup/shutdown lifecycle.
    """
    validate_server_properties(properties)
    selected_server = server if server is not None else select_server(properties.type)
    if properties.ssl_cert_reqs == "required" and getattr(selected_server, "supports_client_auth", False) is not True:
        raise ValueError("Selected server does not support required TLS client authentication")
    selected_loop = event_loop if event_loop is not None else select_event_loop(properties.event_loop)
    effective_properties = replace(properties, event_loop=selected_loop.name)
    os.environ["_PYFLY_SERVER_HOST"] = properties.host
    os.environ["_PYFLY_SERVER_PORT"] = str(properties.port)
    os.environ["_PYFLY_SERVER_TYPE"] = selected_server.server_info.name
    os.environ["_PYFLY_EVENT_LOOP"] = selected_loop.name
    os.environ["_PYFLY_HTTP"] = properties.http
    selected_loop.install()
    selected_server.serve(app, effective_properties)
