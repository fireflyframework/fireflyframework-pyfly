# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the webhooks module."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.webhooks.event_listener import InMemoryWebhookEventStore, WebhookEventStore
from pyfly.webhooks.processor import WebhookProcessor


@auto_configuration
@conditional_on_property("pyfly.webhooks.enabled", having_value="true")
class WebhooksAutoConfiguration:
    @bean
    def webhook_event_store(self, config: dict[str, object] | None = None) -> WebhookEventStore:
        """Build the idempotency store selected by ``pyfly.webhooks.idempotency.provider``.

        Supported values (default ``in-memory``):

        * ``in-memory`` — :class:`~pyfly.webhooks.event_listener.InMemoryWebhookEventStore`
          (single-process; no extra dependencies).
        * ``redis`` — :class:`~pyfly.webhooks.redis_event_store.RedisWebhookEventStore`
          backed by a ``redis.asyncio`` client built from
          ``pyfly.webhooks.idempotency.redis.url`` (default ``redis://localhost:6379/0``)
          with a TTL of ``pyfly.webhooks.idempotency.ttl-seconds`` seconds
          (default ``86400``).  Requires the ``redis`` extra (``redis[asyncio]``).
        """
        cfg = config or {}
        provider = str(cfg.get("pyfly.webhooks.idempotency.provider", "in-memory"))

        if provider == "redis":
            try:
                import redis.asyncio as aioredis
            except ImportError as exc:
                msg = (
                    "pyfly.webhooks.idempotency.provider=redis requires the 'redis' "
                    "package (pip install redis[asyncio])"
                )
                raise ValueError(msg) from exc

            from pyfly.webhooks.redis_event_store import RedisWebhookEventStore

            redis_url = str(cfg.get("pyfly.webhooks.idempotency.redis.url", "redis://localhost:6379/0"))
            ttl = int(str(cfg.get("pyfly.webhooks.idempotency.ttl-seconds", "86400")))
            client = aioredis.from_url(redis_url)
            return RedisWebhookEventStore(client, ttl_seconds=ttl)

        # Default: in-memory (no extra dependencies)
        return InMemoryWebhookEventStore()

    @bean
    def webhook_processor(self, store: WebhookEventStore) -> WebhookProcessor:
        return WebhookProcessor(event_store=store)
