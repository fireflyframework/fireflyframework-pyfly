# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the notifications module — wires dummy providers."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.notifications.providers.dummy import (
    DummyEmailProvider,
    DummyPushProvider,
    DummySmsProvider,
)
from pyfly.notifications.services import (
    DefaultEmailService,
    DefaultPushService,
    DefaultSmsService,
)


@auto_configuration
@conditional_on_property("pyfly.notifications.enabled", having_value="true")
class NotificationsAutoConfiguration:
    @bean
    def email_provider(self) -> DummyEmailProvider:
        return DummyEmailProvider()

    @bean
    def email_service(self, provider: DummyEmailProvider) -> DefaultEmailService:
        return DefaultEmailService(provider=provider)

    @bean
    def sms_provider(self) -> DummySmsProvider:
        return DummySmsProvider()

    @bean
    def sms_service(self, provider: DummySmsProvider) -> DefaultSmsService:
        return DefaultSmsService(provider=provider)

    @bean
    def push_provider(self) -> DummyPushProvider:
        return DummyPushProvider()

    @bean
    def push_service(self, provider: DummyPushProvider) -> DefaultPushService:
        return DefaultPushService(provider=provider)
