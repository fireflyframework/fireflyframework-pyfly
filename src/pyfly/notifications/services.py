# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Default notification services that delegate to a provider adapter.

Template / preference / metrics wiring
---------------------------------------
All three optional capabilities follow the same injection pattern — pass them
to the constructor or leave them ``None`` to keep existing behaviour unchanged.

Template rendering (email only)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Precedence when ``template_engine`` is injected:

1. **Engine present + ``message.template_id`` set** — ``engine.render(id, data)``
   is called and the result is written to ``message.body_html``.  The provider
   receives an ordinary ``body_html`` message; ``template_id`` / ``template_data``
   are cleared so provider-native template routing is NOT triggered.
2. **No engine** (default) — ``message.template_id`` / ``template_data`` are
   forwarded untouched, enabling provider-native template routing (e.g. SendGrid
   Dynamic Templates).

Opt-out / suppression
^^^^^^^^^^^^^^^^^^^^^^
If a ``preference_service`` is injected and the primary recipient has opted out
of the relevant channel, a :data:`~pyfly.notifications.models.EmailStatus.SUPPRESSED`
result is returned immediately **without** calling the provider.

Metrics
^^^^^^^
If a ``metrics`` recorder is injected, the following counters are incremented:

* ``pyfly_notifications_sent_total`` (labels: ``channel``, ``provider``) — on SENT.
* ``pyfly_notifications_failed_total`` (labels: ``channel``, ``provider``) — on FAILED.
* ``pyfly_notifications_suppressed_total`` (labels: ``channel``) — on SUPPRESSED.

Counters are created once in ``__init__`` to avoid repeated registry lookups.
"""

from __future__ import annotations

from typing import Any, cast

from pyfly.notifications.models import (
    EmailMessage,
    EmailStatus,
    NotificationResult,
    PushMessage,
    SmsMessage,
)
from pyfly.notifications.ports import (
    EmailProvider,
    PushProvider,
    SmsProvider,
)


async def _send_safely(provider: Any, message: Any) -> NotificationResult:
    """Delegate to the provider, converting an exception into a FAILED result.

    Provider exceptions become a structured FAILED NotificationResult rather
    than propagating to the caller (audit #36), matching the Java contract.
    """
    try:
        return cast(NotificationResult, await provider.send(message))
    except Exception as exc:  # noqa: BLE001
        return NotificationResult(
            id=message.id,
            provider=getattr(provider, "name", "unknown"),
            status=EmailStatus.FAILED,
            error=str(exc),
        )


class DefaultEmailService:
    def __init__(
        self,
        provider: EmailProvider,
        *,
        template_engine: Any | None = None,
        preference_service: Any | None = None,
        metrics: Any | None = None,
    ) -> None:
        self._provider = provider
        self._template_engine = template_engine
        self._preference_service = preference_service

        # Create metric handles once; they are no-ops when metrics is None.
        if metrics is not None:
            self._sent = metrics.counter(
                "pyfly_notifications_sent_total",
                "Notification sends that succeeded",
                labels=["channel", "provider"],
            )
            self._failed = metrics.counter(
                "pyfly_notifications_failed_total",
                "Notification sends that failed",
                labels=["channel", "provider"],
            )
            self._suppressed = metrics.counter(
                "pyfly_notifications_suppressed_total",
                "Notification sends suppressed by opt-out",
                labels=["channel"],
            )
        else:
            self._sent = None
            self._failed = None
            self._suppressed = None

    async def send(self, message: EmailMessage) -> NotificationResult:
        # ------------------------------------------------------------------
        # 1. Opt-out check
        # ------------------------------------------------------------------
        if self._preference_service is not None and message.to:
            recipient = message.to[0]
            opted_in: bool = await self._preference_service.is_opted_in(recipient, "email")
            if not opted_in:
                if self._suppressed is not None:
                    self._suppressed.labels(channel="email").inc()
                return NotificationResult(
                    id=message.id,
                    provider=getattr(self._provider, "name", "unknown"),
                    status=EmailStatus.SUPPRESSED,
                )

        # ------------------------------------------------------------------
        # 2. Local template rendering (takes priority over provider-native)
        # ------------------------------------------------------------------
        if self._template_engine is not None and message.template_id:
            rendered: str = await self._template_engine.render(message.template_id, message.template_data)
            message.body_html = rendered
            # Clear provider-native routing to avoid double-processing.
            message.template_id = None
            message.template_data = {}

        # ------------------------------------------------------------------
        # 3. Send via provider
        # ------------------------------------------------------------------
        result = await _send_safely(self._provider, message)

        # ------------------------------------------------------------------
        # 4. Metrics
        # ------------------------------------------------------------------
        if result.status == EmailStatus.SENT and self._sent is not None:
            self._sent.labels(channel="email", provider=result.provider).inc()
        elif result.status == EmailStatus.FAILED and self._failed is not None:
            self._failed.labels(channel="email", provider=result.provider).inc()

        return result


class DefaultSmsService:
    def __init__(
        self,
        provider: SmsProvider,
        *,
        preference_service: Any | None = None,
        metrics: Any | None = None,
    ) -> None:
        self._provider = provider
        self._preference_service = preference_service

        if metrics is not None:
            self._sent = metrics.counter(
                "pyfly_notifications_sent_total",
                "Notification sends that succeeded",
                labels=["channel", "provider"],
            )
            self._failed = metrics.counter(
                "pyfly_notifications_failed_total",
                "Notification sends that failed",
                labels=["channel", "provider"],
            )
            self._suppressed = metrics.counter(
                "pyfly_notifications_suppressed_total",
                "Notification sends suppressed by opt-out",
                labels=["channel"],
            )
        else:
            self._sent = None
            self._failed = None
            self._suppressed = None

    async def send(self, message: SmsMessage) -> NotificationResult:
        if self._preference_service is not None and message.to:
            opted_in = await self._preference_service.is_opted_in(message.to, "sms")
            if not opted_in:
                if self._suppressed is not None:
                    self._suppressed.labels(channel="sms").inc()
                return NotificationResult(
                    id=message.id,
                    provider=getattr(self._provider, "name", "unknown"),
                    status=EmailStatus.SUPPRESSED,
                )

        result = await _send_safely(self._provider, message)

        if result.status == EmailStatus.SENT and self._sent is not None:
            self._sent.labels(channel="sms", provider=result.provider).inc()
        elif result.status == EmailStatus.FAILED and self._failed is not None:
            self._failed.labels(channel="sms", provider=result.provider).inc()

        return result


class DefaultPushService:
    def __init__(
        self,
        provider: PushProvider,
        *,
        preference_service: Any | None = None,
        metrics: Any | None = None,
    ) -> None:
        self._provider = provider
        self._preference_service = preference_service

        if metrics is not None:
            self._sent = metrics.counter(
                "pyfly_notifications_sent_total",
                "Notification sends that succeeded",
                labels=["channel", "provider"],
            )
            self._failed = metrics.counter(
                "pyfly_notifications_failed_total",
                "Notification sends that failed",
                labels=["channel", "provider"],
            )
            self._suppressed = metrics.counter(
                "pyfly_notifications_suppressed_total",
                "Notification sends suppressed by opt-out",
                labels=["channel"],
            )
        else:
            self._sent = None
            self._failed = None
            self._suppressed = None

    async def send(self, message: PushMessage) -> NotificationResult:
        if self._preference_service is not None and message.device_tokens:
            opted_in = await self._preference_service.is_opted_in(message.device_tokens[0], "push")
            if not opted_in:
                if self._suppressed is not None:
                    self._suppressed.labels(channel="push").inc()
                return NotificationResult(
                    id=message.id,
                    provider=getattr(self._provider, "name", "unknown"),
                    status=EmailStatus.SUPPRESSED,
                )

        result = await _send_safely(self._provider, message)

        if result.status == EmailStatus.SENT and self._sent is not None:
            self._sent.labels(channel="push", provider=result.provider).inc()
        elif result.status == EmailStatus.FAILED and self._failed is not None:
            self._failed.labels(channel="push", provider=result.provider).inc()

        return result
