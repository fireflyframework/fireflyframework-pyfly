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
"""Entity auditing, backend-neutral: who changed an entity, and when.

The ports are Spring Data's:

- :class:`AuditorAware` answers who is writing (``created_by``/``updated_by``). The default,
  :class:`SecurityContextAuditorAware`, returns the authenticated user of the
  :class:`~pyfly.security.context_holder.SecurityContextHolder`: the user of the HTTP request whichever
  filter authenticated it (bearer token, HTTP Basic, X.509, a session from form login, OAuth2 login or
  switch-user), or the principal a scheduled job, a message listener or a shell command declares with
  :func:`run_as`.
- :class:`DateTimeProvider` answers when (``created_at``/``updated_at``). The default,
  :class:`CurrentDateTimeProvider`, is the UTC clock.

Declare a bean of either port to replace the default (a ``"system"`` fallback auditor, a fixed clock in
tests)::

    @configuration
    class AuditingConfig:
        @bean
        def auditor_aware(self) -> AuditorAware:
            return SystemFallbackAuditor()          # implements get_current_auditor()

:class:`AuditingHandler` stamps entities with them. The backends' hooks (the relational
``AuditingEntityListener``, the document backend's) consult the handler registered by the running
application (:func:`active_auditing_handler`); ``pyfly.data.auditing.enabled=false`` registers none.
"""

from __future__ import annotations

import functools
import inspect
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import Token
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------------------------------------


@runtime_checkable
class AuditorAware(Protocol):
    """Who is doing the current write: Spring Data's ``AuditorAware``.

    Return the auditor's identifier (a user id, ``"system"``), or ``None`` when there is none. It may be an
    ``async def``. It runs while an entity is flushed, so it must not use the session being flushed.
    """

    def get_current_auditor(self) -> str | None | Awaitable[str | None]: ...


@runtime_checkable
class DateTimeProvider(Protocol):
    """When the current write happens: Spring Data's ``DateTimeProvider``. Return an aware ``datetime``
    (a naive one is taken as UTC)."""

    def get_now(self) -> datetime: ...


class SecurityContextAuditorAware(AuditorAware):
    """The default auditor: the authenticated user of the
    :class:`~pyfly.security.context_holder.SecurityContextHolder`, or ``None``."""

    def get_current_auditor(self) -> str | None:
        return SecurityContextHolder.get_authenticated_user_id()


class CurrentDateTimeProvider(DateTimeProvider):
    """The default clock: ``datetime.now(UTC)``."""

    def get_now(self) -> datetime:
        return datetime.now(UTC)


# ---------------------------------------------------------------------------------------------------------
# The handler the backends' hooks use
# ---------------------------------------------------------------------------------------------------------


class AuditingHandler:
    """Stamps entities with the application's :class:`AuditorAware` and :class:`DateTimeProvider`
    (Spring Data's ``AuditingHandler``).

    One handler is *active* at a time, the one registered last (:meth:`register`); the backends' hooks
    consult :func:`active_auditing_handler`. Registering is idempotent, and :meth:`unregister` hands over to
    the handler registered before (two application contexts in one process, one after the other).
    """

    def __init__(
        self, auditor_aware: AuditorAware | None = None, date_time_provider: DateTimeProvider | None = None
    ) -> None:
        self._auditor_aware: AuditorAware = auditor_aware or SecurityContextAuditorAware()
        self._date_time_provider: DateTimeProvider = date_time_provider or CurrentDateTimeProvider()

    @property
    def auditor_aware(self) -> AuditorAware:
        return self._auditor_aware

    @property
    def date_time_provider(self) -> DateTimeProvider:
        return self._date_time_provider

    def now(self) -> datetime:
        """The provider's current time, as an aware UTC ``datetime``."""
        now = self._date_time_provider.get_now()
        return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)

    def current_auditor_or_awaitable(self) -> str | None | Awaitable[str | None]:
        """What the :class:`AuditorAware` returned: the auditor, or an awaitable of it (an ``async``
        auditor). A synchronous hook resolves the awaitable its backend's way."""
        return self._auditor_aware.get_current_auditor()

    async def current_auditor(self) -> str | None:
        """The current auditor (an ``async`` :class:`AuditorAware` is awaited)."""
        auditor = self._auditor_aware.get_current_auditor()
        if inspect.isawaitable(auditor):
            return await auditor
        return auditor

    def register(self) -> None:
        """Make this the active handler (idempotent)."""
        with _LOCK:
            if self in _HANDLERS:
                _HANDLERS.remove(self)
            _HANDLERS.append(self)
            self._activated()

    def unregister(self) -> None:
        """Stop being a registered handler; the one registered before it is active again (idempotent)."""
        with _LOCK:
            if self not in _HANDLERS:
                return
            _HANDLERS.remove(self)
            self._deactivated(remaining=len(_HANDLERS))

    @property
    def registered(self) -> bool:
        """Whether this handler is registered (it is active when it was registered last)."""
        return self in _HANDLERS

    async def start(self) -> None:
        """Lifecycle start: register (a no-op when the bean registered itself already)."""
        self.register()

    async def stop(self) -> None:
        """Lifecycle stop: unregister, so a stopped application context leaves nothing behind."""
        self.unregister()

    def _activated(self) -> None:
        """Hook for a backend subclass, called under the registration lock when the handler registers."""

    def _deactivated(self, *, remaining: int) -> None:
        """Hook for a backend subclass, called under the registration lock when the handler unregisters;
        *remaining* is how many handlers are still registered."""


_LOCK = threading.RLock()
_HANDLERS: list[AuditingHandler] = []


def active_auditing_handler() -> AuditingHandler | None:
    """The handler the running application registered last, or ``None`` (auditing disabled)."""
    handlers = _HANDLERS
    return handlers[-1] if handlers else None


def registered_auditing_handlers() -> tuple[AuditingHandler, ...]:
    """Every registered handler, the active one last."""
    return tuple(_HANDLERS)


async def current_auditor() -> str | None:
    """The current auditor of the active handler, or ``None`` when auditing is disabled. For a bulk
    statement that stamps ``updated_by`` itself."""
    handler = active_auditing_handler()
    return await handler.current_auditor() if handler is not None else None


# ---------------------------------------------------------------------------------------------------------
# run_as
# ---------------------------------------------------------------------------------------------------------


class RunAs:
    """A block, or every call of a function, that runs as a given principal (:func:`run_as`)."""

    def __init__(self, context: SecurityContext) -> None:
        self._context = context
        self._tokens: list[Token[SecurityContext | None]] = []

    @property
    def context(self) -> SecurityContext:
        return self._context

    def __enter__(self) -> SecurityContext:
        self._tokens.append(SecurityContextHolder.set_context(self._context))
        return self._context

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        SecurityContextHolder.reset_context(self._tokens.pop())

    def __call__(self, function: F) -> F:
        """Decorate *function* (``async def`` or not) so that every call runs as the principal."""
        context = self._context
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                with _using(context):
                    return await function(*args, **kwargs)

            return cast(F, run_async)

        @functools.wraps(function)
        def run_sync(*args: Any, **kwargs: Any) -> Any:
            with _using(context):
                return function(*args, **kwargs)

        return cast(F, run_sync)


@contextmanager
def _using(context: SecurityContext) -> Iterator[None]:
    token = SecurityContextHolder.set_context(context)
    try:
        yield
    finally:
        SecurityContextHolder.reset_context(token)


def run_as(principal: str | SecurityContext) -> RunAs:
    """Run as *principal* (a user id, or a full :class:`~pyfly.security.context.SecurityContext`): as a
    block, or as a decorator of a function (a ``@scheduled`` job, a message listener, a shell command).
    Auditing records it as the auditor, and :class:`~pyfly.security.context_holder.SecurityContextHolder`
    returns it inside the block::

        @scheduled(cron="0 3 * * *")
        @run_as("system:retention")
        async def purge(self) -> None: ...

        with run_as("system:import"):
            await importer.load(rows)
    """
    context = principal if isinstance(principal, SecurityContext) else SecurityContext(user_id=principal)
    return RunAs(context)
