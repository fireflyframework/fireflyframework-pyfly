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

import asyncio
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


def _report_abandoned_deletion(task: asyncio.Task[None]) -> None:
    """Log the failure of a session deletion whose request was cancelled (nobody awaits it any more)."""
    if not task.cancelled() and task.exception() is not None:
        logger.warning("session_delete_failed", exc_info=task.exception())


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
    and moved to its new id through ``rename`` when the request rotated it (``rotate_id()``, a
    privilege elevation), only while the store still holds it. When it was logged out, evicted or
    expired while the request ran, the change is dropped, the session counts as invalidated, and the
    response sets no session cookie at all: the request neither brings the session back, under its id
    or a new one, nor sends its cookie again, and it does not clear the cookie either (another request
    of the same browser, a login in another tab, may have set a new one meanwhile). A new session, and
    one rotated by a login (``rotate_id(on_login=True)``: a fresh authentication stands even if the old
    entry is gone), is inserted with ``save`` and its old id deleted. A store without ``replace`` and
    ``rename`` gets every change through ``save``, an insert-or-replace, and cannot tell a revoked
    session from a live one: the filter logs ``session_store_without_replace`` (a WARNING) when it is
    built on one.

    **Deletions run to their end.** The deletion of an invalidated session (a logout) and of a rotated
    session's old id runs in a task of its own, shielded from the request's cancellation: a
    level-triggered cancel scope, as anyio's, cancels every await of the request's cleanup too, and a
    logout cancelled that way left the session live.

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
        self._conditional = store if isinstance(store, ConditionalSessionStore) else None
        if self._conditional is None:
            # Such a store cannot keep a logout or an eviction final (see the class documentation).
            logger.warning("session_store_without_replace", extra={"store": type(store).__name__})
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
        stored = session.stored_id
        # The id the store holds the session under, when a rotation moved the session away from it (however
        # many rotations came before this persist): it must stop resolving to the session (anti-fixation).
        stale = stored if stored is not None and stored != session.id else None

        if session.invalidated:
            await self._delete_shielded([session_id for session_id in (stale, session.id) if session_id is not None])
            return True
        if not session.modified:
            return True
        conditional = self._conditional
        if conditional is not None and stored is not None and not session.rotated_on_login:
            data = session.get_data()
            if stale is None:
                written = await conditional.replace(session.id, data, self._ttl)
            else:
                written = await conditional.rename(stale, session.id, data, self._ttl)
            if not written:
                # Logged out, evicted or expired while this request ran: never bring it back, under its id or a
                # new one.
                logger.debug("session_ended_during_request")
                session.invalidate()
                return False
        else:
            # A new session, a login's rotation (a fresh authentication stands even if the old entry is gone),
            # or a store that cannot write conditionally.
            if stale is not None:
                await self._delete_shielded([stale])
            await self._store.save(session.id, session.get_data(), self._ttl)
        session.mark_persisted()
        return True

    async def _delete_shielded(self, session_ids: list[str]) -> None:
        """Delete *session_ids* from the store in a task of its own that no cancellation of the request stops.

        A logout, or a login refused or failed, invalidates the session and relies on this deletion to end it;
        a rotation relies on it to retire the old id. When the request is cancelled (the client went away), a
        level-triggered cancel scope, as anyio's, cancels every later await of the request too, this one
        included: shielded, the deletion runs to its end even so. It runs outside any unit of work the request
        has bound (:func:`~pyfly.data.transaction.detached`).
        """
        from pyfly.data.transaction import detached

        async def delete_all() -> None:
            for session_id in session_ids:
                await self._store.delete(session_id)

        task = detached(delete_all(), name="session-delete")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_report_abandoned_deletion)
            raise

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
