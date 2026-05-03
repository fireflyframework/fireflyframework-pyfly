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
"""Instance registry for admin server mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class InstanceInfo:
    """Represents a registered application instance."""

    name: str
    url: str
    status: str = "UNKNOWN"
    last_checked: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
        return {
            "name": self.name,
            "url": self.url,
            "status": self.status,
            "last_checked": (self.last_checked.isoformat() if self.last_checked else None),
            "metadata": self.metadata,
        }


class InstanceRegistry:
    """Maintains a registry of known application instances."""

    def __init__(self) -> None:
        self._instances: dict[str, InstanceInfo] = {}

    def register(self, name: str, url: str, metadata: dict[str, Any] | None = None) -> InstanceInfo:
        """Register a new instance (or overwrite an existing one)."""
        info = InstanceInfo(
            name=name,
            url=url.rstrip("/"),
            metadata=metadata or {},
        )
        self._instances[name] = info
        return info

    def deregister(self, name: str) -> bool:
        """Remove an instance by name. Returns True if it existed."""
        return self._instances.pop(name, None) is not None

    def get_instances(self) -> list[InstanceInfo]:
        """Return all registered instances."""
        return list(self._instances.values())

    def get_instance(self, name: str) -> InstanceInfo | None:
        """Look up an instance by name."""
        return self._instances.get(name)

    def update_status(self, name: str, status: str) -> None:
        """Update the status and last_checked timestamp for an instance."""
        inst = self._instances.get(name)
        if inst is not None:
            inst.status = status
            inst.last_checked = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full registry."""
        return {
            "instances": [inst.to_dict() for inst in self._instances.values()],
        }
