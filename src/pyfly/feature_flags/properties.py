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
"""``pyfly.feature-flags.*`` (spec 8). The ``flags``/``evaluators`` maps are not bound here: the config source reads
them with ``Config.get_section`` so flag keys keep their spelling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyfly.core.config import config_properties

if TYPE_CHECKING:
    from pyfly.core.config import Config

__all__ = [
    "PREFIX",
    "ContextProperties",
    "EventsProperties",
    "FeatureFlagsProperties",
    "FileSourceProperties",
    "HttpSourceProperties",
    "ManagementProperties",
    "OpenFeatureProperties",
    "ServerProperties",
    "SourcesProperties",
    "StoreSourceProperties",
    "WebProperties",
]

PREFIX = "pyfly.feature-flags"
_DISABLED_STATUSES = (404, 403, 503)
_STORE_DRIVERS = ("database", "memory")
_FILE_SUFFIXES = (".json", ".yaml", ".yml")


@dataclass
class OpenFeatureProperties:
    domain: str = ""


@dataclass
class FileSourceProperties:
    enabled: bool = False
    path: str = ""
    refresh_interval: str = "5s"


@dataclass
class HttpSourceProperties:
    enabled: bool = False
    url: str = ""
    token: str = ""
    refresh_interval: str = "30s"
    timeout: str = "2s"


@dataclass
class StoreSourceProperties:
    enabled: bool = False
    driver: str = "database"
    refresh_interval: str = "5s"
    datasource: str = ""


@dataclass
class SourcesProperties:
    file: FileSourceProperties = field(default_factory=FileSourceProperties)
    http: HttpSourceProperties = field(default_factory=HttpSourceProperties)
    store: StoreSourceProperties = field(default_factory=StoreSourceProperties)


@dataclass
class ContextProperties:
    tenant_attribute: str = "tenant"
    trust_tenant_header: bool = False


@dataclass
class WebProperties:
    disabled_status: int = 404


@dataclass
class EventsProperties:
    evaluations: bool = False


@dataclass
class ManagementProperties:
    writes: bool = False


@dataclass
class ServerProperties:
    enabled: bool = False
    path: str = "/feature-flags/flagd.json"
    token: str = ""
    allow_anonymous: bool = False


@config_properties(prefix=PREFIX)
@dataclass
class FeatureFlagsProperties:
    """The feature-flag settings (defaults in ``pyfly-defaults.yaml``).

    Only the settings of an enabled source are checked. A polling source's ``refresh-interval`` must be greater than
    0: the registry polls only a positive interval, so ``0`` would silently mean "never refresh".
    """

    enabled: bool = False
    openfeature: OpenFeatureProperties = field(default_factory=OpenFeatureProperties)
    sources: SourcesProperties = field(default_factory=SourcesProperties)
    context: ContextProperties = field(default_factory=ContextProperties)
    web: WebProperties = field(default_factory=WebProperties)
    events: EventsProperties = field(default_factory=EventsProperties)
    management: ManagementProperties = field(default_factory=ManagementProperties)
    server: ServerProperties = field(default_factory=ServerProperties)

    @classmethod
    def from_config(cls, config: Config) -> FeatureFlagsProperties:
        """Bind ``pyfly.feature-flags`` and validate it (a ``ValueError`` names the broken key)."""
        try:
            properties = config.bind(cls)
        except (TypeError, ValueError) as error:  # a value binding cannot convert (disabled-status: abc)
            raise ValueError(f"{PREFIX}: {error}") from error
        properties.validate()
        return properties

    @staticmethod
    def seconds(value: str | int | float, key: str) -> float:
        """A duration setting in seconds (``5s``, ``500ms``, ``1m``, ``2h``, or a number of seconds), greater than 0.

        *key* is the setting's key below ``pyfly.feature-flags`` (``sources.file.refresh-interval``); a
        ``ValueError`` names it in full.
        """
        from pyfly.resilience.registry import parse_duration

        try:
            if isinstance(value, bool):  # a bool is an int: `true` would read as one second
                raise ValueError(f"not a duration: {value!r}")
            seconds = parse_duration(value).total_seconds()
        except (ArithmeticError, TypeError, ValueError) as error:  # unparsable, NaN, infinite
            raise ValueError(f"{PREFIX}.{key} must be a duration such as '5s' or '500ms', got {value!r}") from error
        if seconds <= 0:
            raise ValueError(f"{PREFIX}.{key} must be greater than 0, got {value!r}")
        return seconds

    def validate(self) -> None:
        """Refuse an invalid setting with a ``ValueError`` naming its full key."""
        if self.web.disabled_status not in _DISABLED_STATUSES:
            raise ValueError(f"{PREFIX}.web.disabled-status must be 404, 403 or 503, got {self.web.disabled_status!r}")
        sources = self.sources
        if sources.store.driver not in _STORE_DRIVERS:
            raise ValueError(f"{PREFIX}.sources.store.driver must be database or memory, got {sources.store.driver!r}")
        if sources.file.enabled:
            if not sources.file.path:
                raise ValueError(f"{PREFIX}.sources.file.path is required when the file source is enabled")
            if not str(sources.file.path).lower().endswith(_FILE_SUFFIXES):
                raise ValueError(f"{PREFIX}.sources.file.path must name a .json, .yaml or .yml file")
            self.seconds(sources.file.refresh_interval, "sources.file.refresh-interval")
        if sources.http.enabled:
            if not sources.http.url:
                raise ValueError(f"{PREFIX}.sources.http.url is required when the http source is enabled")
            self.seconds(sources.http.refresh_interval, "sources.http.refresh-interval")
            self.seconds(sources.http.timeout, "sources.http.timeout")
        if sources.store.enabled:
            self.seconds(sources.store.refresh_interval, "sources.store.refresh-interval")
        if self.server.enabled:
            if not self.server.token and not self.server.allow_anonymous:
                raise ValueError(
                    f"{PREFIX}.server.enabled requires {PREFIX}.server.token "
                    f"(or {PREFIX}.server.allow-anonymous=true to serve the flags without one)"
                )
            if not str(self.server.path).startswith("/"):
                raise ValueError(f"{PREFIX}.server.path must start with /, got {self.server.path!r}")
