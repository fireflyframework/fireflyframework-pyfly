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
"""ActuatorEndpoint protocol — extensible actuator endpoint interface."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ActuatorEndpoint(Protocol):
    """Protocol for actuator management endpoints.

    Each endpoint is exposed at ``/actuator/{endpoint_id}``.
    Implement this protocol directly or as a ``@component`` bean for
    auto-discovery.
    """

    @property
    def endpoint_id(self) -> str:
        """URL path suffix: ``/actuator/{endpoint_id}``."""
        ...

    @property
    def enabled(self) -> bool:
        """Default enable state.  Can be overridden via config."""
        ...

    async def handle(self, context: Any = None) -> dict[str, Any]:
        """Handle a request to this endpoint and return a JSON-serializable dict."""
        ...
