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

import logging
import uuid
from typing import Any

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.session.ports.outbound import ConditionalSessionStore, SessionStore
from pyfly.session.session import HttpSession
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

_DEFAULT_COOKIE_NAME = "PYFLY_SESSION"
_DEFAULT_TTL = 1800  # 30 minutes


class SessionFilter(OncePerRequestFilter):
    """Manages server-side sessions via a configurable cookie.

    Reads the session cookie from the incoming request, loads session data
    from the ``SessionStore``, attaches the ``HttpSession`` to
    ``request.state.session``, and persists changes after the response.

    **What is saved.** A new session, and a session changed through the ``HttpSession`` API
    (``set_attribute``, ``remove_attribute``, ``rotate_id``; ``invalidate`` deletes it). A value
    mutated in place (an item appended to a list attribute, say) is saved only along with such a
    change; once the session was saved, an in-place mutation alone is not saved again.

    **A revoked session stays revoked.** A session the store is known to hold
    (:attr:`HttpSession.stored_id`: loaded by this request, or already saved by it) is written back
    through the store's ``replace`` (:class:`~pyfly.session.ports.outbound.ConditionalSessionStore`),
    only while the store still holds it. When it was logged out, evicted or expired while the request
    ran, the change is dropped, the session counts as invalidated, and the response sets no session
    cookie at all: the request neither brings the session back nor sends its cookie again, and it
    does not clear the cookie either (another request of the same browser, a login in another tab,
    may have set a new one meanwhile). A new or rotated id is inserted with ``save``. A store without
    ``replace`` gets every change through ``save``, an insert-or-replace, and cannot tell a revoked
    session from a live one.

    ``request.state.persist_session`` saves the session at once (a coroutine function taking no
    arguments): the OAuth2 login handler saves the session it has just logged in before it registers
    the session with the concurrency controller, which counts a session while the store has it. A
    save leaves the session unmodified (:meth:`HttpSession.mark_persisted`), so the persist that runs
    when the handler returns writes it again only for a later change, and then only while the store
    still holds it: a concurrent login of the same principal may have evicted it meanwhile.
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 150

    def __init__(
        self,
        store: SessionStore,
        cookie_name: str = _DEFAULT_COOKIE_NAME,
        ttl: int = _DEFAULT_TTL,
        secure: bool = False,
    ) -> None:
        self._store = store
        # Writes a session only while the store holds it; None for a store that cannot.
        self._replace = store.replace if isinstance(store, ConditionalSessionStore) else None
        self._cookie_name = cookie_name
        self._ttl = ttl
        self._secure = secure

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        session = await self._load_or_create_session(request)
        request.state.session = session
        # Set once a write-back finds the session revoked: nothing is persisted, and no cookie is sent, after that.
        ended = False

        async def persist_session() -> None:
            nonlocal ended
            if not ended:
                ended = not await self._persist_session(session)

        request.state.persist_session = persist_session
        # Expose the session to the container for SESSION-scoped bean resolution.
        from pyfly.context.request_context import HTTP_SESSION_KEY, RequestContext

        ctx = RequestContext.current()
        if ctx is not None:
            ctx.set(HTTP_SESSION_KEY, session)

        try:
            response = await call_next(request)
        finally:
            await persist_session()

        if ended:
            # Revoked while this request ran: neither its cookie again nor a deletion, which could clear the
            # cookie another request of the same browser set meanwhile (a login in another tab rotating it).
            return response

        # Issue the cookie for a new session, and re-issue it for an existing,
        # still-valid session so its max-age slides forward on each access
        # (audit #52). The Secure attribute is configurable (default off for
        # local HTTP development).
        if not session.invalidated:
            response.set_cookie(
                key=self._cookie_name,
                value=session.id,
                httponly=True,
                secure=self._secure or self._is_secure_request(request),
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

    async def _persist_session(self, session: HttpSession) -> bool:
        """Save or delete the session in the store based on its state (see the class documentation); a saved
        session is left unmodified until its next change. ``False`` when the write-back of a session the store
        was known to hold found it gone (revoked while the request ran): the session is then invalidated, and
        nothing was written."""
        # If the id was rotated (e.g. on login), drop the pre-rotation entry so a
        # fixed/stale id can no longer resolve to this session (anti-fixation).
        if session.previous_id is not None and session.previous_id != session.id:
            await self._store.delete(session.previous_id)

        if session.invalidated:
            await self._store.delete(session.id)
            return True
        if not session.modified:
            return True
        if self._replace is not None and session.stored_id == session.id:
            if not await self._replace(session.id, session.get_data(), self._ttl):
                # Logged out, evicted or expired while this request ran: never bring it back.
                logger.debug("session_ended_during_request")
                session.invalidate()
                return False
        else:
            await self._store.save(session.id, session.get_data(), self._ttl)
        session.mark_persisted()
        return True

    @staticmethod
    def _is_secure_request(request: Any) -> bool:
        """Whether the request arrived over HTTPS (honoring ``X-Forwarded-Proto``).

        Sets the cookie ``Secure`` attribute automatically in production (HTTPS)
        without breaking plain-HTTP local development.
        """
        headers = getattr(request, "headers", None)
        forwarded = ""
        if headers is not None and hasattr(headers, "get"):
            forwarded = headers.get("x-forwarded-proto", "")
        if forwarded:
            return str(forwarded).split(",")[0].strip().lower() == "https"
        url = getattr(request, "url", None)
        return url is not None and getattr(url, "scheme", "") == "https"
