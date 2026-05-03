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
"""CQRS configuration properties bound from YAML.

Mirrors Java's ``CqrsProperties`` and ``AuthorizationProperties``.

YAML structure::

    pyfly:
      cqrs:
        enabled: true
        command:
          timeout: 30
          metrics_enabled: true
          tracing_enabled: true
        query:
          timeout: 15
          caching_enabled: true
          cache_ttl: 900
          metrics_enabled: true
        authorization:
          enabled: true
          custom:
            enabled: true
            timeout_ms: 5000
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyfly.core.config import config_properties


@dataclass
class CommandProperties:
    """``pyfly.cqrs.command.*``."""

    timeout: int = 30
    metrics_enabled: bool = True
    tracing_enabled: bool = True


@dataclass
class QueryProperties:
    """``pyfly.cqrs.query.*``."""

    timeout: int = 15
    caching_enabled: bool = True
    cache_ttl: int = 900
    metrics_enabled: bool = True
    tracing_enabled: bool = True


@dataclass
class CustomAuthorizationProperties:
    """``pyfly.cqrs.authorization.custom.*``."""

    enabled: bool = True
    timeout_ms: int = 5000


@dataclass
class AuthorizationProperties:
    """``pyfly.cqrs.authorization.*``."""

    enabled: bool = True
    custom: CustomAuthorizationProperties = field(default_factory=CustomAuthorizationProperties)


@config_properties(prefix="pyfly.cqrs")
@dataclass
class CqrsProperties:
    """Root CQRS configuration (``pyfly.cqrs.*``)."""

    enabled: bool = True
    command: CommandProperties = field(default_factory=CommandProperties)
    query: QueryProperties = field(default_factory=QueryProperties)
    authorization: AuthorizationProperties = field(default_factory=AuthorizationProperties)
