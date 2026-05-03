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
"""Outbound port: web server interface."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WebServerPort(Protocol):
    """Abstract web server interface.

    Any web framework adapter (Starlette, Flask, etc.) must implement this protocol.
    """

    def create_app(self, **kwargs: Any) -> Any:
        """Create and return a web application instance."""
        ...
