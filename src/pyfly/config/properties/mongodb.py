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
"""Document data subsystem configuration properties (``pyfly.data.document.*``).

:meth:`DocumentProperties.from_config` is how the document auto-configuration reads them (relaxed binding:
``max-pool-size`` and ``max_pool_size`` are the same key, and a ``PYFLY_*`` environment variable wins), and
:meth:`DocumentProperties.client_options` is what the ``AsyncMongoClient`` is built with: the pool settings,
the timeouts (in seconds here, converted to pymongo's milliseconds), the application name, ``tz_aware=True``
(timestamps come back as aware UTC values) and ``uuidRepresentation="standard"`` (UUIDs stored as BSON
binary subtype 4, what other drivers read), then the free-form ``options`` map, passed to the client as it is
and winning over the rest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyfly.core.config import config_properties

if TYPE_CHECKING:
    from pyfly.core.config import Config

PREFIX = "pyfly.data.document"
"""The configuration prefix of the document data subsystem."""

_UUID_REPRESENTATIONS = frozenset({"standard", "pythonLegacy", "javaLegacy", "csharpLegacy", "unspecified"})


@dataclass
class DocumentTransactionProperties:
    """``pyfly.data.document.transaction.*``: the options of the MongoDB transactions."""

    read_concern: str | None = None
    """``snapshot``, ``majority``, ``local``... (``None``: the client's)."""
    write_concern: str | None = None
    """``majority`` or a number of nodes (``None``: the client's)."""
    max_commit_time: float | None = None
    """Seconds a commit may take on the server (``maxCommitTimeMS``); a ``timeout=`` of the unit wins."""
    default: bool | None = None
    """Whether the document datasource is the default of ``@transactional`` (a boundary that names none). By
    default it is when the relational data layer is not enabled."""


@dataclass
class DocumentHealthProperties:
    """``pyfly.data.document.health.*``."""

    timeout: float = 2.0
    """Seconds the readiness check waits for the server's answer to ``ping``."""


@config_properties(prefix=PREFIX)
@dataclass
class DocumentProperties:
    """Configuration for the document data subsystem (pyfly.data.document.*)."""

    enabled: bool = False
    uri: str = "mongodb://localhost:27017"
    database: str = "pyfly"
    datasource: str = "document"
    """The datasource name of the document units of work (``@transactional(datasource=...)``)."""
    min_pool_size: int = 0
    max_pool_size: int = 100
    max_idle_time: float | None = None
    """Seconds a pooled connection may stay idle before it is closed (``maxIdleTimeMS``)."""
    connect_timeout: float | None = None
    """Seconds to open a connection (``connectTimeoutMS``; pymongo's default is 20)."""
    server_selection_timeout: float | None = None
    """Seconds to find a server for an operation (``serverSelectionTimeoutMS``; pymongo's default is 30)."""
    socket_timeout: float | None = None
    """Seconds a socket read or write may take (``socketTimeoutMS``; none by default)."""
    wait_queue_timeout: float | None = None
    """Seconds an operation waits for a pooled connection (``waitQueueTimeoutMS``; none by default)."""
    app_name: str | None = None
    """The application name the server logs and ``currentOp`` show (``appname``)."""
    tz_aware: bool = True
    uuid_representation: str = "standard"
    options: dict[str, Any] = field(default_factory=dict)
    """Any other ``AsyncMongoClient`` keyword argument, passed as it is (``retryWrites``, ``readPreference``...)."""
    models: list[str] = field(default_factory=list)
    """Document classes, or modules and packages to scan for them, by dotted name: initialized with the
    documents the repositories name."""
    transaction: DocumentTransactionProperties = field(default_factory=DocumentTransactionProperties)
    health: DocumentHealthProperties = field(default_factory=DocumentHealthProperties)

    @classmethod
    def from_config(cls, config: Config) -> DocumentProperties:
        """Bind ``pyfly.data.document.*`` from *config* and validate it (``ValueError`` names the key)."""
        properties = config.bind(cls)
        properties.models = _names(properties.models, f"{PREFIX}.models")
        if not isinstance(properties.options, Mapping):
            raise ValueError(f"{PREFIX}.options must be a map of AsyncMongoClient options, got {properties.options!r}")
        properties.options = dict(properties.options)
        for key in ("min_pool_size", "max_pool_size"):
            if int(getattr(properties, key)) < 0:
                raise ValueError(f"{PREFIX}.{key.replace('_', '-')} must not be negative")
        if properties.uuid_representation not in _UUID_REPRESENTATIONS:
            raise ValueError(
                f"{PREFIX}.uuid-representation must be one of {sorted(_UUID_REPRESENTATIONS)}, "
                f"got {properties.uuid_representation!r}"
            )
        if not properties.datasource.strip():
            raise ValueError(f"{PREFIX}.datasource must name the document datasource")
        return properties

    def client_options(self) -> dict[str, Any]:
        """The ``AsyncMongoClient`` keyword arguments (module documentation)."""
        options: dict[str, Any] = {
            "minPoolSize": int(self.min_pool_size),
            "maxPoolSize": int(self.max_pool_size),
            "tz_aware": bool(self.tz_aware),
            "uuidRepresentation": self.uuid_representation,
        }
        for key, value in (
            ("maxIdleTimeMS", self.max_idle_time),
            ("connectTimeoutMS", self.connect_timeout),
            ("serverSelectionTimeoutMS", self.server_selection_timeout),
            ("socketTimeoutMS", self.socket_timeout),
            ("waitQueueTimeoutMS", self.wait_queue_timeout),
        ):
            if value is not None:
                options[key] = max(1, int(float(value) * 1000))
        if self.app_name:
            options["appname"] = self.app_name
        options.update(self.options)
        return options


def _names(value: Any, key: str) -> list[str]:
    """A list of dotted names: a YAML list, or one comma-separated string (an environment variable)."""
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(f"{key} must be a list of dotted names, got {value!r}")
    return [str(item).strip() for item in items if str(item).strip()]
