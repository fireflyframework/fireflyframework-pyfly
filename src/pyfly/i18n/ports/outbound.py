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
"""MessageSource protocol — port for resolving internationalised messages."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MessageSource(Protocol):
    """Abstract message-resolution interface.

    All message backends (resource bundles, database-backed, etc.) must
    implement this protocol.
    """

    def get_message(
        self,
        code: str,
        args: tuple[Any, ...] = (),
        locale: str = "en",
    ) -> str:
        """Resolve *code* for the given *locale*, substituting *args*.

        Raises ``KeyError`` when the code cannot be resolved.
        """
        ...

    def get_message_or_default(
        self,
        code: str,
        default: str,
        args: tuple[Any, ...] = (),
        locale: str = "en",
    ) -> str:
        """Resolve *code* for the given *locale*, returning *default* on miss."""
        ...
