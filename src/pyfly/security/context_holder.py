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
"""The security context of the running task: Spring's ``SecurityContextHolder``.

One place answers "who is doing this?" for code that is not handed the request: auditing
(``AuditorAware``), and any service that needs the principal. :meth:`SecurityContextHolder.get_context`
returns, in this order:

1. a context set on the holder for the current task (:meth:`SecurityContextHolder.set_context`,
   :meth:`SecurityContextHolder.using`, or ``pyfly.data.auditing.run_as``): scheduled jobs, message
   listeners, shell commands and tests say who they act as this way;
2. inside an HTTP request, the context the security filters established for it
   (``request.state.security_context``), read live, so whichever filter authenticated the request (a
   bearer token, HTTP Basic, X.509, the session a form login, an OAuth2 login or a switch-user stored)
   is the one seen, including a filter that sets only ``request.state``;
3. ``RequestContext.current().security_context``, the older bridge some filters and applications set.

Values set on the holder follow ``contextvars`` rules: a task started from the block inherits them, and
leaving the block restores the previous one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from pyfly.security.context import SecurityContext

REQUEST_STATE_ATTRIBUTE = "__pyfly_request_state__"
"""The ``RequestContext`` attribute under which ``RequestContextFilter`` keeps the request's ``state``, so
the holder reads the context the security filters establish while the request runs."""

_context_var: ContextVar[SecurityContext | None] = ContextVar("pyfly_security_context_holder", default=None)


class SecurityContextHolder:
    """Access to the :class:`~pyfly.security.context.SecurityContext` of the running task (module
    documentation)."""

    @staticmethod
    def get_context() -> SecurityContext | None:
        """The security context of the running task, or ``None`` when nothing established one."""
        explicit = _context_var.get()
        if explicit is not None:
            return explicit
        # Imported here: pyfly.context.request_context imports pyfly.security, which exports this holder.
        from pyfly.context.request_context import RequestContext

        request_context = RequestContext.current()
        if request_context is None:
            return None
        state: Any = request_context.get(REQUEST_STATE_ATTRIBUTE)
        established = getattr(state, "security_context", None) if state is not None else None
        if isinstance(established, SecurityContext):
            return established
        return request_context.security_context

    @staticmethod
    def get_authenticated_user_id() -> str | None:
        """The ``user_id`` of the current context when it is authenticated, otherwise ``None``."""
        context = SecurityContextHolder.get_context()
        if context is None or not context.is_authenticated:
            return None
        return context.user_id

    @staticmethod
    def set_context(context: SecurityContext | None) -> Token[SecurityContext | None]:
        """Set the context of the current task (and of the tasks it starts from now on). Pass the returned
        token to :meth:`reset_context` to restore the previous one; ``None`` falls back to the request's."""
        return _context_var.set(context)

    @staticmethod
    def reset_context(token: Token[SecurityContext | None]) -> None:
        """Restore the context :meth:`set_context` replaced."""
        _context_var.reset(token)

    @staticmethod
    @contextmanager
    def using(context: SecurityContext) -> Iterator[SecurityContext]:
        """Run a block with *context* as the current security context."""
        token = _context_var.set(context)
        try:
            yield context
        finally:
            _context_var.reset(token)
