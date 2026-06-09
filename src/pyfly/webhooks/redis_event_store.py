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
"""Redis-backed :class:`~pyfly.webhooks.event_listener.WebhookEventStore`.

Hexagonal: the async Redis client is **injected** by the composition root; this
module never imports ``redis`` directly so the dependency is optional.  The
``redis.asyncio`` client is passed as ``Any`` — duck-typed so mypy --strict
remains satisfied without a hard ``redis`` type-stub dependency.
"""

from __future__ import annotations

from typing import Any


class RedisWebhookEventStore:
    """Distributed, durable webhook idempotency store backed by Redis.

    Idempotency keys are stored as simple string keys with an expiry TTL so
    that the store self-prunes without a background job.

    .. note::
        The :meth:`already_processed` + :meth:`remember` pair is a two-step
        check-then-set (non-atomic).  For the vast majority of webhook workloads
        this is acceptable — duplicate delivery is rare and the window between
        the two calls is negligible.  If strict once-exactly semantics are
        required, callers should wrap both calls with a distributed lock.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        key_prefix: str = "webhook:idem:",
        ttl_seconds: int = 86400,
    ) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._ttl = ttl_seconds

    async def already_processed(self, idempotency_key: str) -> bool:
        """Return ``True`` if *idempotency_key* was previously stored in Redis."""
        result: int = await self._redis.exists(self._prefix + idempotency_key)
        return bool(result)

    async def remember(self, idempotency_key: str) -> None:
        """Store *idempotency_key* in Redis with the configured TTL."""
        await self._redis.set(self._prefix + idempotency_key, "1", ex=self._ttl)
