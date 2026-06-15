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
"""Management server configuration properties (Spring management.server.* parity)."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.management.server")
@dataclass
class ManagementServerProperties:
    """Configuration for the separate management server (pyfly.management.server.*).

    Mirrors Spring Boot's ``management.server.*``:

    - ``port``: a positive port different from the application port runs the
      actuator endpoints (``/actuator/*``) and the admin dashboard (``/admin``)
      on a dedicated in-process listener. Equal to the app port collapses to a
      single shared port; ``-1`` disables the management web endpoints entirely.
      In ``pyfly-defaults.yaml`` this defaults to ``9090`` (separate from the app
      port ``8080``), so a real boot exposes two ports out of the box. ``None``
      (the dataclass default, used by raw-dict ``Config`` in tests) means shared.
    - ``address``: bind interface for the separate listener (e.g. ``127.0.0.1``
      to keep it node-local). Defaults to the application host.
    - ``base_path``: path prefix on the separate listener; the existing
      ``pyfly.management.endpoints.web.base-path`` (default ``/actuator``) nests
      under it.
    """

    port: int | None = None
    address: str | None = None
    base_path: str = ""
