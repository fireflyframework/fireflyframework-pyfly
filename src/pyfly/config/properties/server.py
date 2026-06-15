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
"""Application server configuration properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyfly.core.config import config_properties

if TYPE_CHECKING:
    from pyfly.core.config import Config


@dataclass
class GranianProperties:
    """Granian-specific tuning (pyfly.server.granian.*)."""

    runtime_threads: int = 1
    runtime_mode: str = "auto"
    backpressure: int | None = None
    respawn_failed_workers: bool = True


@config_properties(prefix="pyfly.server")
@dataclass
class ServerProperties:
    """Configuration for the application server (pyfly.server.*).

    ``host`` / ``port`` are the Spring ``server.address`` / ``server.port``
    parity keys and the *only* way to configure the application listener — the
    former ``pyfly.web.host`` / ``pyfly.web.port`` keys were removed.
    """

    host: str = "0.0.0.0"
    port: int = 8080
    type: str = "auto"
    event_loop: str = "auto"
    workers: int = 1
    backlog: int = 1024
    graceful_timeout: int = 30
    http: str = "auto"
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    keep_alive_timeout: int = 5
    max_concurrent_connections: int | None = None
    max_requests_per_worker: int | None = None
    granian: GranianProperties = field(default_factory=GranianProperties)


def resolve_app_port(config: Config) -> int:
    """Resolve the application HTTP port (Spring ``server.port`` parity).

    Reads ``pyfly.server.port`` (which ``Config.get`` resolves from the
    ``PYFLY_SERVER_PORT`` env var, config files, then the ``8080`` default).
    """
    return int(config.get("pyfly.server.port", 8080))


def resolve_app_host(config: Config) -> str:
    """Resolve the application bind host (Spring ``server.address`` parity).

    Reads ``pyfly.server.host`` (env ``PYFLY_SERVER_HOST`` / config / default).
    """
    return str(config.get("pyfly.server.host", "0.0.0.0"))
