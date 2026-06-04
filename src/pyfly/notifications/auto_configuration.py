# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the notifications module.

Provider selection (``pyfly.notifications.{email,sms,push}.provider``) builds the
real adapter from config (SendGrid/Resend/SMTP, Twilio, Firebase), falling back
to an in-memory Dummy provider when none is configured (audit #30). Each
provider bean is exposed under its port type so the services inject the port.
"""

from __future__ import annotations

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
    def email_service(self, provider: EmailProvider) -> DefaultEmailService:
        return DefaultEmailService(provider=provider)

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
    def sms_service(self, provider: SmsProvider) -> DefaultSmsService:
        return DefaultSmsService(provider=provider)

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
    def push_service(self, provider: PushProvider) -> DefaultPushService:
        return DefaultPushService(provider=provider)
