# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the webhooks module."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.webhooks.event_listener import InMemoryWebhookEventStore
from pyfly.webhooks.processor import WebhookProcessor


@auto_configuration
@conditional_on_property("pyfly.webhooks.enabled", having_value="true")
class WebhooksAutoConfiguration:
    @bean
    def webhook_event_store(self) -> InMemoryWebhookEventStore:
        return InMemoryWebhookEventStore()

    @bean
    def webhook_processor(self, store: InMemoryWebhookEventStore) -> WebhookProcessor:
        return WebhookProcessor(event_store=store)
