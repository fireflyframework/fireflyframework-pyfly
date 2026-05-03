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
"""SessionFilter — loads and persists HTTP sessions via cookies."""

from __future__ import annotations

import uuid
from typing import Any

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.session.ports.outbound import SessionStore
from pyfly.session.session import HttpSession
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

_DEFAULT_COOKIE_NAME = "PYFLY_SESSION"
_DEFAULT_TTL = 1800  # 30 minutes


class SessionFilter(OncePerRequestFilter):
    """Manages server-side sessions via a configurable cookie.

    Reads the session cookie from the incoming request, loads session data
    from the ``SessionStore``, attaches the ``HttpSession`` to
    ``request.state.session``, and persists changes after the response.
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 150

    def __init__(
        self,
        store: SessionStore,
        cookie_name: str = _DEFAULT_COOKIE_NAME,
        ttl: int = _DEFAULT_TTL,
    ) -> None:
        self._store = store
        self._cookie_name = cookie_name
        self._ttl = ttl

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        session = await self._load_or_create_session(request)
        request.state.session = session

        try:
            response = await call_next(request)
        finally:
            await self._persist_session(session)

        if session.is_new and not session.invalidated:
            response.set_cookie(
                key=self._cookie_name,
                value=session.id,
                httponly=True,
                samesite="lax",
                max_age=self._ttl,
            )

        if session.invalidated:
            response.delete_cookie(key=self._cookie_name)

        return response

    async def _load_or_create_session(self, request: Any) -> HttpSession:
        """Load an existing session from the store or create a new one."""
        cookies = getattr(request, "cookies", {})
        session_id = cookies.get(self._cookie_name)

        if session_id:
            data = await self._store.get(session_id)
            if data is not None:
                return HttpSession(session_id, data)

        new_id = uuid.uuid4().hex
        return HttpSession(new_id, is_new=True)

    async def _persist_session(self, session: HttpSession) -> None:
        """Save or delete the session in the store based on its state."""
        if session.invalidated:
            await self._store.delete(session.id)
        elif session.modified:
            await self._store.save(session.id, session.get_data(), self._ttl)
