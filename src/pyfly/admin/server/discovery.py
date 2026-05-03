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
"""Instance discovery strategies for admin server mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.admin.server.instance_registry import InstanceRegistry


class StaticDiscovery:
    """Loads instances from a static list of configuration dicts.

    Each dict must contain at least ``name`` and ``url`` keys.
    An optional ``metadata`` key may provide additional key-value pairs.

    Example config::

        instances:
          - name: app-1
            url: http://localhost:8080
          - name: app-2
            url: http://localhost:8081
    """

    def __init__(self, instances: list[dict[str, Any]], registry: InstanceRegistry) -> None:
        self._instances = instances
        self._registry = registry

    def discover(self) -> None:
        """Register all statically configured instances."""
        for entry in self._instances:
            name = entry.get("name", "")
            url = entry.get("url", "")
            if name and url:
                metadata = entry.get("metadata") or {}
                self._registry.register(name, url, metadata)
