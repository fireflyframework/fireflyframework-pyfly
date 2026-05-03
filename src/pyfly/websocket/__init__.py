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
"""PyFly WebSocket — decorator-driven WebSocket support for controllers.

Usage::

    from pyfly.websocket import WebSocketSession, websocket_mapping

    @rest_controller
    @request_mapping("/ws")
    class EchoController:

        @websocket_mapping("/echo")
        async def echo(self, session: WebSocketSession) -> None:
            await session.accept()
            while True:
                msg = await session.receive_text()
                await session.send_text(f"echo: {msg}")
"""

from pyfly.websocket.decorators import websocket_mapping
from pyfly.websocket.handler import WebSocketHandler, WebSocketSession

__all__ = [
    "WebSocketHandler",
    "WebSocketSession",
    "websocket_mapping",
]
