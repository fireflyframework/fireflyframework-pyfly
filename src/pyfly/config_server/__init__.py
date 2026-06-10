# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly config server — Spring Cloud Config Server analogue.

Two surfaces:

* **Server side** (``ConfigServer``): exposes ``/{app}/{profile}/{label}``
  endpoints and pulls config from a backend.
* **Client side** (``ConfigClient``): fetches config at startup, merges into
  the application's :class:`pyfly.core.Config`.

Two backends ship out of the box:

* :class:`~pyfly.config_server.backend.FilesystemConfigBackend` — reads/writes
  YAML/JSON files under a configurable directory tree; supports tiered
  search locations (e.g. ``[domain, core, common]``) with higher-precedence
  locations overriding lower ones.
* :class:`~pyfly.config_server.adapters.git.GitConfigBackend` — clones a Git
  repository and delegates file reads to ``FilesystemConfigBackend`` over the
  working tree.  Requires ``pip install pyfly[config-server-git]``.

To add Consul, Vault, etcd, or any other store, implement
:class:`~pyfly.config_server.backend.ConfigBackend` (a ``Protocol`` with
``fetch``, ``save``, and ``list`` methods).
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
    "GitConfigBackend",
    "InMemoryConfigBackend",
]


def __getattr__(name: str) -> object:
    if name == "GitConfigBackend":
        from pyfly.config_server.adapters.git import GitConfigBackend  # noqa: PLC0415

        return GitConfigBackend
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
