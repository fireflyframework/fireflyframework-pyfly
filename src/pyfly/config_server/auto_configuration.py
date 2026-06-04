# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the config-server module."""

from __future__ import annotations

import tempfile

from pyfly.config_server.backend import FilesystemConfigBackend
from pyfly.config_server.server import ConfigServer
from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config


@auto_configuration
@conditional_on_property("pyfly.config-server.enabled", having_value="true")
class ConfigServerAutoConfiguration:
    @bean
    def config_backend(self, config: Config) -> FilesystemConfigBackend:
        # Persist under a configured root so saved config survives restarts and an
        # operator can point the server at a real config directory (audit #88).
        # Fall back to a throwaway tempdir only when nothing is configured.
        root = config.get("pyfly.config-server.backend.root") or config.get(
            "pyfly.config-server.native.search-locations"
        )
        return FilesystemConfigBackend(str(root) if root else tempfile.mkdtemp(prefix="pyfly-config-"))

    @bean
    def config_server(self, backend: FilesystemConfigBackend) -> ConfigServer:
        return ConfigServer(backend=backend)
