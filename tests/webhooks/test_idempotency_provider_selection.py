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
"""The webhook idempotency store must be selectable via config (regression: the bean
previously took a ``dict`` param, which the DI container map-injected, so ``redis`` could
never be chosen)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pyfly.core.config import Config
from pyfly.webhooks.auto_configuration import WebhooksAutoConfiguration
from pyfly.webhooks.event_listener import InMemoryWebhookEventStore


def test_default_provider_is_in_memory() -> None:
    store = WebhooksAutoConfiguration().webhook_event_store(Config({}))
    assert isinstance(store, InMemoryWebhookEventStore)


def test_redis_provider_selected_from_config() -> None:
    from pyfly.webhooks.redis_event_store import RedisWebhookEventStore

    config = Config(
        {
            "pyfly": {
                "webhooks": {
                    "idempotency": {
                        "provider": "redis",
                        "redis": {"url": "redis://localhost:6379/1"},
                        "ttl-seconds": 120,
                    }
                }
            }
        }
    )
    with patch("redis.asyncio.from_url", return_value=MagicMock()) as from_url:
        store = WebhooksAutoConfiguration().webhook_event_store(config)
    assert isinstance(store, RedisWebhookEventStore)
    from_url.assert_called_once_with("redis://localhost:6379/1")
