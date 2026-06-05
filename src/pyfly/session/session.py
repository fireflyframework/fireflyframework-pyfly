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
"""HttpSession — server-side session wrapper."""

from __future__ import annotations

import time
import uuid
from typing import Any


class HttpSession:
    """Wraps a session data dictionary with convenience accessors.

    Attributes:
        id: The unique session identifier.
        is_new: ``True`` if the session was created during the current request.
    """

    def __init__(
        self,
        session_id: str,
        data: dict[str, Any] | None = None,
        *,
        is_new: bool = False,
    ) -> None:
        self._id = session_id
        self._data: dict[str, Any] = data if data is not None else {}
        self._is_new = is_new
        self._invalidated = False
        self._modified = is_new
        self._previous_id: str | None = None

        now = time.time()
        if "_created_at" not in self._data:
            self._data["_created_at"] = now
        self._data["_last_accessed"] = now

    @property
    def id(self) -> str:
        return self._id

    @property
    def previous_id(self) -> str | None:
        """The id this session was rotated away from (set by :meth:`rotate_id`)."""
        return self._previous_id

    @property
    def is_new(self) -> bool:
        return self._is_new

    @property
    def created_at(self) -> float:
        return float(self._data["_created_at"])

    @property
    def last_accessed(self) -> float:
        return float(self._data["_last_accessed"])

    @property
    def invalidated(self) -> bool:
        return self._invalidated

    @property
    def modified(self) -> bool:
        return self._modified

    def get_attribute(self, name: str) -> Any | None:
        """Return the session attribute value, or ``None`` if absent."""
        return self._data.get(name)

    def set_attribute(self, name: str, value: Any) -> None:
        """Set a session attribute."""
        self._data[name] = value
        self._modified = True

    def remove_attribute(self, name: str) -> None:
        """Remove a session attribute if it exists."""
        if name in self._data:
            del self._data[name]
            self._modified = True

    def get_attribute_names(self) -> list[str]:
        """Return all attribute names, excluding internal metadata keys."""
        return [k for k in self._data if not k.startswith("_")]

    def rotate_id(self) -> None:
        """Assign a fresh session id, preserving all data.

        Call on authentication / privilege elevation to prevent session-fixation
        attacks: an attacker who fixed the victim's pre-auth session id cannot
        ride the authenticated session. The store entry and cookie are migrated
        to the new id when the session is persisted by the ``SessionFilter``.
        """
        if self._invalidated:
            return
        self._previous_id = self._id
        self._id = uuid.uuid4().hex
        self._modified = True

    def invalidate(self) -> None:
        """Mark the session for deletion."""
        self._invalidated = True
        self._modified = True

    def get_data(self) -> dict[str, Any]:
        """Return the raw session data dictionary."""
        return self._data
