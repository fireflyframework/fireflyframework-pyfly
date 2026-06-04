# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Callback dispatcher — fan an event out to every matching subscription."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
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


# Statuses worth retrying — transient server/timeout/rate-limit only; a 4xx
# (other than 408/429) is a permanent client error and is not retried (#194).
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _is_retryable(status: int) -> bool:
    return status in _RETRYABLE_STATUS or status >= 500


def _is_authorized(target_url: str, domains: Any) -> bool:
    """Return True if *target_url*'s host matches one of the authorized domains."""
    from urllib.parse import urlparse

    host = (urlparse(target_url).hostname or "").lower()
    if not host:
        return False
    for entry in domains:
        allowed = getattr(entry, "domain", entry).lower().strip()
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


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

        # SSRF protection: when an allowlist is configured, the target host must
        # match one of the authorized domains before any request is made (#190).
        if config.authorized_domains and not _is_authorized(sub.target_url, config.authorized_domains):
            execution.status = CallbackStatus.FAILED
            execution.last_error = "Domain not authorized"
            await self._executions.save(execution)
            return execution

        headers = dict(sub.headers)
        if config.secret:
            # Sign the canonical JSON serialization of the payload — NOT
            # ``str(payload)`` (Python dict repr with single quotes / ``True`` /
            # ``None``), which is invalid JSON and unverifiable by any receiver.
            # Receivers verify with HmacSignatureValidator over this same compact,
            # key-sorted JSON body (see pyfly.webhooks.signature).
            canonical_body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            sig = hmac.new(
                config.secret.encode("utf-8"),
                canonical_body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Pyfly-Signature"] = f"sha256={sig}"
        headers.setdefault("Content-Type", "application/json")

        for attempt in range(1, config.max_attempts + 1):
            execution.attempts = attempt
            retryable = True
            try:
                status = await self._http(sub.target_url, payload, headers)
                execution.response_status = status
                if 200 <= status < 300:
                    execution.status = CallbackStatus.DELIVERED
                    execution.delivered_at = datetime.now(UTC)
                    break
                execution.last_error = f"http {status}"
                retryable = _is_retryable(status)  # 4xx (except 408/429) is permanent (#194)
            except Exception as exc:  # noqa: BLE001 — transport errors are retryable
                execution.last_error = str(exc)
            if not retryable:
                execution.status = CallbackStatus.FAILED
                break
            if attempt < config.max_attempts:
                # Exponential backoff (capped at 5 min), matching Java (#194).
                delay_ms = min(config.backoff_ms * (2 ** (attempt - 1)), 300_000)
                await asyncio.sleep(delay_ms / 1000.0)
        else:
            execution.status = CallbackStatus.FAILED

        await self._executions.save(execution)
        return execution
