# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Per-channel notification opt-out / preference service.

Usage in the send flow
----------------------
Before delegating to the provider, :class:`~pyfly.notifications.services.DefaultEmailService`
(and the SMS/Push equivalents) check ``is_opted_in(recipient, channel)``.  If the
recipient has opted out, the send is short-circuited and a
:class:`~pyfly.notifications.models.NotificationResult` with status ``SUPPRESSED``
is returned **without** calling the provider.

The *channel* values used internally are ``"email"``, ``"sms"``, and ``"push"``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotificationPreferenceService(Protocol):
    """Port for querying per-recipient, per-channel notification preferences."""

    async def is_opted_in(self, recipient: str, channel: str) -> bool:
        """Return ``True`` if *recipient* has NOT opted out of *channel*.

        Parameters
        ----------
        recipient:
            A channel-specific identifier — an email address for ``"email"``,
            a phone number for ``"sms"``, or a device token for ``"push"``.
        channel:
            One of ``"email"``, ``"sms"``, ``"push"``.
        """
        ...


class InMemoryPreferenceService:
    """Thread-safe in-memory implementation of :class:`NotificationPreferenceService`.

    All recipients are opted-in by default.  Call :meth:`opt_out` to suppress
    future sends and :meth:`opt_in` to restore them.

    Example
    -------
    >>> import asyncio
    >>> svc = InMemoryPreferenceService()
    >>> svc.opt_out("alice@example.com", "email")
    >>> asyncio.run(svc.is_opted_in("alice@example.com", "email"))
    False
    >>> asyncio.run(svc.is_opted_in("alice@example.com", "sms"))
    True
    """

    def __init__(self) -> None:
        # Set of (recipient, channel) tuples that are opted OUT.
        self._opted_out: set[tuple[str, str]] = set()

    def opt_out(self, recipient: str, channel: str) -> None:
        """Record that *recipient* has opted out of *channel*."""
        self._opted_out.add((recipient, channel))

    def opt_in(self, recipient: str, channel: str) -> None:
        """Remove the opt-out record for *recipient* / *channel*."""
        self._opted_out.discard((recipient, channel))

    async def is_opted_in(self, recipient: str, channel: str) -> bool:
        """Return ``True`` unless the recipient has explicitly opted out."""
        return (recipient, channel) not in self._opted_out
