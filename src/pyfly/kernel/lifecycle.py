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
"""Unified lifecycle protocol for infrastructure adapters.

Analogous to Spring's Lifecycle interface. All infrastructure ports that own
connections, pools, or external resources should extend this protocol.
The framework calls start() during context startup and stop() during shutdown.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Lifecycle(Protocol):
    """Standard lifecycle for infrastructure adapters.

    Adapters that own connections, pools, or external resources implement
    this protocol. The ApplicationContext calls start() during startup
    and stop() during shutdown -- in registration order and reverse order
    respectively.
    """

    async def start(self) -> None:
        """Initialize connections and validate connectivity.

        Called during application startup. Infrastructure adapters should
        establish their connections here. If the connection fails, raise
        an exception -- the framework wraps it in BeanCreationException
        for fail-fast behavior.
        """
        ...

    async def stop(self) -> None:
        """Release connections and clean up resources.

        Called during application shutdown. Best-effort cleanup -- exceptions
        are logged but do not prevent shutdown of other adapters.
        """
        ...
