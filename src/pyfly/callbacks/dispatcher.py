# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Callback dispatcher — fan an event out to every matching subscription."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pyfly.callbacks.models import (
    CallbackConfig,
    CallbackExecution,
    CallbackStatus,
    CallbackSubscription,
)
from pyfly.callbacks.repository import (
    CallbackConfigRepository,
    CallbackExecutionRepository,
)

_logger = logging.getLogger(__name__)

HttpSender = Callable[[str, dict[str, Any], dict[str, str]], Awaitable[int]]


async def _default_sender(url: str, payload: dict[str, Any], headers: dict[str, str]) -> int:
    """No-op default — adapters override with httpx / aiohttp."""
    _logger.info("would POST %s headers=%s body=%s", url, headers, payload)
    return 200


class CallbackDispatcher:
    """Dispatches events to all matching :class:`CallbackSubscription`s."""

    def __init__(
        self,
        configs: CallbackConfigRepository,
        executions: CallbackExecutionRepository,
        *,
        http: HttpSender | None = None,
    ) -> None:
        self._configs = configs
        self._executions = executions
        self._http = http or _default_sender

    async def dispatch(self, tenant_id: str, event_type: str, payload: dict[str, Any]) -> list[CallbackExecution]:
        configs = await self._configs.list_by_tenant(tenant_id)
        results: list[CallbackExecution] = []
        for config in configs:
            if not config.enabled:
                continue
            for sub in config.subscriptions:
                if sub.event_type != event_type and sub.event_type != "*":
                    continue
                exec_record = await self._deliver(config, sub, event_type, payload)
                results.append(exec_record)
        return results

    async def _deliver(
        self,
        config: CallbackConfig,
        sub: CallbackSubscription,
        event_type: str,
        payload: dict[str, Any],
    ) -> CallbackExecution:
        execution = CallbackExecution(
            config_id=config.id,
            event_type=event_type,
            target_url=sub.target_url,
            payload=payload,
        )
        await self._executions.save(execution)

        headers = dict(sub.headers)
        if config.secret:
            sig = hmac.new(
                config.secret.encode("utf-8"),
                str(payload).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Pyfly-Signature"] = f"sha256={sig}"
        headers.setdefault("Content-Type", "application/json")

        for attempt in range(1, config.max_attempts + 1):
            execution.attempts = attempt
            try:
                status = await self._http(sub.target_url, payload, headers)
                execution.response_status = status
                if 200 <= status < 300:
                    execution.status = CallbackStatus.DELIVERED
                    execution.delivered_at = datetime.now(UTC)
                    break
                execution.last_error = f"http {status}"
            except Exception as exc:  # noqa: BLE001
                execution.last_error = str(exc)
            if attempt < config.max_attempts:
                await asyncio.sleep(config.backoff_ms / 1000.0)
        else:
            execution.status = CallbackStatus.FAILED

        await self._executions.save(execution)
        return execution
