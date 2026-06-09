# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the notifications module.

Provider selection (``pyfly.notifications.{email,sms,push}.provider``) builds the
real adapter from config (SendGrid/Resend/SMTP, Twilio, Firebase), falling back
to an in-memory Dummy provider when none is configured (audit #30). Each
provider bean is exposed under its port type so the services inject the port.

Template engine (``pyfly.notifications.template.engine``)
----------------------------------------------------------
* ``jinja2`` — a :class:`~pyfly.notifications.template.Jinja2TemplateEngine` is
  built (requires ``pyfly[notifications]``).  Bean is omitted when jinja2 is not
  installed.
* ``none`` or absent — no template engine bean; services fall back to
  provider-native template routing.

Preference store (``pyfly.notifications.preference.store``)
------------------------------------------------------------
* ``memory`` — an :class:`~pyfly.notifications.preferences.InMemoryPreferenceService`
  is registered.
* ``none`` or absent — no preference service; opt-out suppression is disabled.

Metrics
-------
An optional :class:`~pyfly.observability.ports.MetricsRecorder` is injected into
each service bean when one is present in the container.  Absent → no-op.
"""

from __future__ import annotations

from typing import Any

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config
from pyfly.notifications.ports import EmailProvider, PushProvider, SmsProvider
from pyfly.notifications.services import (
    DefaultEmailService,
    DefaultPushService,
    DefaultSmsService,
)


@auto_configuration
@conditional_on_property("pyfly.notifications.enabled", having_value="true")
class NotificationsAutoConfiguration:
    @bean
    def email_provider(self, config: Config) -> EmailProvider:
        provider = str(config.get("pyfly.notifications.email.provider", "dummy")).lower()
        if provider == "sendgrid":
            from pyfly.notifications.providers.sendgrid import SendGridEmailProvider

            return SendGridEmailProvider(api_key=str(config.get("pyfly.notifications.email.sendgrid.api-key", "")))
        if provider == "resend":
            from pyfly.notifications.providers.resend import ResendEmailProvider

            return ResendEmailProvider(
                api_key=str(config.get("pyfly.notifications.email.resend.api-key", "")),
                default_from=str(config.get("pyfly.notifications.email.from", "")) or None,
            )
        if provider == "smtp":
            from pyfly.notifications.providers.smtp import SmtpEmailProvider

            return SmtpEmailProvider(
                host=str(config.get("pyfly.notifications.email.smtp.host", "localhost")),
                port=int(config.get("pyfly.notifications.email.smtp.port", 587)),
                username=str(config.get("pyfly.notifications.email.smtp.username", "")) or None,
                password=str(config.get("pyfly.notifications.email.smtp.password", "")) or None,
            )
        from pyfly.notifications.providers.dummy import DummyEmailProvider

        return DummyEmailProvider()

    @bean
    def template_engine(self, config: Config) -> Any:
        """Build a Jinja2TemplateEngine when ``pyfly.notifications.template.engine=jinja2``.

        Returns ``None`` when jinja2 is absent or the config value is ``none``.
        Bean consumers must accept ``| None``.
        """
        engine_cfg = str(config.get("pyfly.notifications.template.engine", "none")).lower()
        if engine_cfg != "jinja2":
            return None
        try:
            import jinja2  # noqa: F401, PLC0415
        except ImportError:
            return None
        from pyfly.notifications.template import Jinja2TemplateEngine

        # An empty template registry — callers register templates at runtime.
        return Jinja2TemplateEngine(templates={})

    @bean
    def preference_service(self, config: Config) -> Any:
        """Build an InMemoryPreferenceService when ``pyfly.notifications.preference.store=memory``.

        Returns ``None`` when disabled.  Bean consumers must accept ``| None``.
        """
        store_cfg = str(config.get("pyfly.notifications.preference.store", "none")).lower()
        if store_cfg != "memory":
            return None
        from pyfly.notifications.preferences import InMemoryPreferenceService

        return InMemoryPreferenceService()

    @bean
    def email_service(
        self,
        provider: EmailProvider,
        template_engine: Any | None = None,
        preference_service: Any | None = None,
        metrics: Any | None = None,
    ) -> DefaultEmailService:
        return DefaultEmailService(
            provider=provider,
            template_engine=template_engine,
            preference_service=preference_service,
            metrics=metrics,
        )

    @bean
    def sms_provider(self, config: Config) -> SmsProvider:
        provider = str(config.get("pyfly.notifications.sms.provider", "dummy")).lower()
        if provider == "twilio":
            from pyfly.notifications.providers.twilio import TwilioSmsProvider

            return TwilioSmsProvider(
                account_sid=str(config.get("pyfly.notifications.sms.twilio.account-sid", "")),
                auth_token=str(config.get("pyfly.notifications.sms.twilio.auth-token", "")),
                from_number=str(config.get("pyfly.notifications.sms.twilio.from-number", "")) or None,
            )
        from pyfly.notifications.providers.dummy import DummySmsProvider

        return DummySmsProvider()

    @bean
    def sms_service(
        self,
        provider: SmsProvider,
        preference_service: Any | None = None,
        metrics: Any | None = None,
    ) -> DefaultSmsService:
        return DefaultSmsService(
            provider=provider,
            preference_service=preference_service,
            metrics=metrics,
        )

    @bean
    def push_provider(self, config: Config) -> PushProvider:
        provider = str(config.get("pyfly.notifications.push.provider", "dummy")).lower()
        if provider == "firebase":
            from pyfly.notifications.providers.firebase import FirebasePushProvider

            return FirebasePushProvider(
                project_id=str(config.get("pyfly.notifications.push.firebase.project-id", "")),
                access_token=str(config.get("pyfly.notifications.push.firebase.access-token", "")),
            )
        from pyfly.notifications.providers.dummy import DummyPushProvider

        return DummyPushProvider()

    @bean
    def push_service(
        self,
        provider: PushProvider,
        preference_service: Any | None = None,
        metrics: Any | None = None,
    ) -> DefaultPushService:
        return DefaultPushService(
            provider=provider,
            preference_service=preference_service,
            metrics=metrics,
        )
