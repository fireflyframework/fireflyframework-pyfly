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
"""HTTP exchanges actuator endpoint — Spring Boot ``/actuator/httpexchanges`` parity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.actuator.http_exchanges import HttpExchangeRecorder


class HttpExchangesEndpoint:
    """Exposes recent HTTP exchanges (newest first) at ``/actuator/httpexchanges``."""

    def __init__(self, recorder: HttpExchangeRecorder) -> None:
        self._recorder = recorder

    @property
    def endpoint_id(self) -> str:
        return "httpexchanges"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        return {"exchanges": self._recorder.recent()}
