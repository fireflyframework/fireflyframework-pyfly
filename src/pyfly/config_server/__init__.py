# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly config server — Spring Cloud Config Server analogue.

Two surfaces:

* **Server side** (``ConfigServer``): exposes ``/{app}/{profile}/{label}``
  endpoints and pulls config from a backend.
* **Client side** (``ConfigClient``): fetches config at startup, merges into
  the application's :class:`pyfly.core.Config`.

The default backend is filesystem-based; subclass :class:`ConfigBackend` to
plug in Git, Consul, etcd, Vault, etc.
"""

from __future__ import annotations

from pyfly.config_server.backend import (
    ConfigBackend,
    ConfigSource,
    FilesystemConfigBackend,
    InMemoryConfigBackend,
)
from pyfly.config_server.client import ConfigClient
from pyfly.config_server.server import ConfigServer

__all__ = [
    "ConfigBackend",
    "ConfigClient",
    "ConfigServer",
    "ConfigSource",
    "FilesystemConfigBackend",
    "InMemoryConfigBackend",
]
