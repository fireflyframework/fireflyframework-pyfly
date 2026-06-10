# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the config-server module."""

from __future__ import annotations

import logging
import tempfile
from typing import Any

from pyfly.config_server.backend import ConfigBackend, FilesystemConfigBackend
from pyfly.config_server.server import ConfigServer
from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config

_logger = logging.getLogger(__name__)


def _build_backend(config: Config) -> ConfigBackend:
    """Select and construct the appropriate backend from config.

    Decision tree
    -------------
    1. ``pyfly.config-server.backend.type == "git"``
       → :class:`~pyfly.config_server.adapters.git.GitConfigBackend`
       (requires ``pyfly[config-server-git]``; falls back to filesystem with a
       warning when GitPython is not installed).
    2. ``pyfly.config-server.backend.search-locations`` is set
       → :class:`~pyfly.config_server.backend.FilesystemConfigBackend` with
       the tiered *search_locations* list (highest-precedence location first;
       e.g. ``[domain, core, common]``).
    3. Otherwise
       → :class:`~pyfly.config_server.backend.FilesystemConfigBackend` with
       a single *root* taken from ``pyfly.config-server.backend.root`` (or the
       legacy ``pyfly.config-server.native.search-locations``), falling back to
       a throwaway tempdir.
    """
    backend_type = str(config.get("pyfly.config-server.backend.type") or "").lower()

    if backend_type == "git":
        git_uri: Any = config.get("pyfly.config-server.backend.git.uri")
        if git_uri:
            try:
                from pyfly.config_server.adapters.git import GitConfigBackend  # noqa: PLC0415

                git_label = str(config.get("pyfly.config-server.backend.git.label") or "main")
                git_clone_dir: Any = config.get("pyfly.config-server.backend.git.clone-dir")
                _logger.info("config-server: using GitConfigBackend (uri=%s label=%s)", git_uri, git_label)
                return GitConfigBackend(
                    str(git_uri),
                    label=git_label,
                    clone_dir=str(git_clone_dir) if git_clone_dir else None,
                )
            except ImportError:
                _logger.warning(
                    "config-server: backend.type=git requested but GitPython is not installed "
                    "(pip install pyfly[config-server-git]); falling back to FilesystemConfigBackend"
                )
        else:
            _logger.warning(
                "config-server: backend.type=git but pyfly.config-server.backend.git.uri is not set; "
                "falling back to FilesystemConfigBackend"
            )

    # Tiered multi-location filesystem backend.
    search_locations_raw: Any = config.get("pyfly.config-server.backend.search-locations")
    if search_locations_raw:
        locations: list[str | Any]
        if isinstance(search_locations_raw, list):
            locations = [str(loc) for loc in search_locations_raw]
        else:
            # Support comma-separated string for YAML scalar values.
            locations = [loc.strip() for loc in str(search_locations_raw).split(",") if loc.strip()]
        if locations:
            _logger.info("config-server: using FilesystemConfigBackend with search-locations=%s", locations)
            return FilesystemConfigBackend(locations[0], search_locations=locations)

    # Single-root filesystem backend (original behaviour).
    root = config.get("pyfly.config-server.backend.root") or config.get("pyfly.config-server.native.search-locations")
    return FilesystemConfigBackend(str(root) if root else tempfile.mkdtemp(prefix="pyfly-config-"))


@auto_configuration
@conditional_on_property("pyfly.config-server.enabled", having_value="true")
class ConfigServerAutoConfiguration:
    @bean
    def config_backend(self, config: Config) -> ConfigBackend:
        return _build_backend(config)

    @bean
    def config_server(self, backend: ConfigBackend) -> ConfigServer:
        return ConfigServer(backend=backend)
