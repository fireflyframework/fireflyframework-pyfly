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
"""winloop event loop adapter — high-performance loop for Windows."""

from __future__ import annotations


class WinloopEventLoopAdapter:
    """winloop event loop — uvloop equivalent for Windows (~5x faster than default)."""

    def install(self) -> None:
        """Install winloop as the default asyncio event loop policy."""
        import winloop  # type: ignore[import-not-found, unused-ignore]

        winloop.install()

    @property
    def name(self) -> str:
        return "winloop"
