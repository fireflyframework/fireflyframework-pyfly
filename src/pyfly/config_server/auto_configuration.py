# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the config-server module."""

from __future__ import annotations

import tempfile

from pyfly.config_server.backend import FilesystemConfigBackend
from pyfly.config_server.server import ConfigServer
from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property


@auto_configuration
@conditional_on_property("pyfly.config-server.enabled", having_value="true")
class ConfigServerAutoConfiguration:
    @bean
    def config_backend(self) -> FilesystemConfigBackend:
        return FilesystemConfigBackend(tempfile.mkdtemp(prefix="pyfly-config-"))

    @bean
    def config_server(self, backend: FilesystemConfigBackend) -> ConfigServer:
        return ConfigServer(backend=backend)
