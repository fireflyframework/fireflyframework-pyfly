# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Post-start wiring that mounts the config-server HTTP routes (audit #83).

The ``ConfigServer`` bean is only instantiated during ``ApplicationContext.start()``
(inside the lifespan, after ``create_app`` returns), so its routes must be
discovered by the post-start rescan — not at ``create_app`` time. Starlette is
NOT imported here; the adapter owns that boundary."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from pyfly.config_server.server import ConfigServer

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


def _enabled(config: Any) -> bool:
    return str(config.get("pyfly.config-server.enabled", "false")).lower() in ("true", "1", "yes")


def build_config_server_routes(context: ApplicationContext | None) -> list[Any]:
    """Return the config-server routes when enabled, else an empty list."""
    if context is None or not _enabled(context.config):
        return []

    server: ConfigServer | None = None
    with contextlib.suppress(Exception):
        server = context.get_bean(ConfigServer)
    if server is None:
        # The bean may be registered under its concrete type only; scan instances.
        for reg in context.container._registrations.values():
            inst = reg.instance
            if isinstance(inst, ConfigServer):
                server = inst
                break
    if server is None:
        return []

    base_path = str(context.config.get("pyfly.config-server.base-path", ""))
    from pyfly.config_server.adapters.starlette import make_starlette_config_server_routes

    return list(make_starlette_config_server_routes(server, base_path))
