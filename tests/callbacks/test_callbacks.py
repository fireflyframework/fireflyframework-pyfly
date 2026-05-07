# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the callbacks module."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.callbacks.dispatcher import CallbackDispatcher
from pyfly.callbacks.models import (
    CallbackConfig,
    CallbackStatus,
    CallbackSubscription,
)
from pyfly.callbacks.repository import (
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)


@pytest.mark.asyncio
async def test_dispatch_delivers_to_matching_subscriptions() -> None:
    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()
    posted: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> int:
        posted.append((url, payload))
        return 200

    config = CallbackConfig(
        tenant_id="t1",
        name="orders",
        subscriptions=[
            CallbackSubscription(event_type="OrderPlaced", target_url="https://example.com/orders"),
            CallbackSubscription(event_type="*", target_url="https://example.com/all"),
        ],
    )
    await configs.save(config)

    dispatcher = CallbackDispatcher(configs, executions, http=fake_post)
    results = await dispatcher.dispatch("t1", "OrderPlaced", {"id": 1})
    assert all(r.status == CallbackStatus.DELIVERED for r in results)
    assert len(posted) == 2


@pytest.mark.asyncio
async def test_dispatcher_signs_with_secret() -> None:
    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()
    captured_headers: list[dict[str, str]] = []

    async def fake_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> int:
        captured_headers.append(headers)
        return 200

    config = CallbackConfig(
        tenant_id="t",
        name="signed",
        secret="topsecret",
        subscriptions=[
            CallbackSubscription(event_type="X", target_url="https://x.example/y"),
        ],
    )
    await configs.save(config)
    dispatcher = CallbackDispatcher(configs, executions, http=fake_post)
    await dispatcher.dispatch("t", "X", {"a": 1})
    assert "X-Pyfly-Signature" in captured_headers[0]


@pytest.mark.asyncio
async def test_failure_marks_execution_failed() -> None:
    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()

    async def always_fails(url: str, payload: dict[str, Any], headers: dict[str, str]) -> int:
        return 500

    config = CallbackConfig(
        tenant_id="t",
        name="bad",
        max_attempts=2,
        backoff_ms=1,
        subscriptions=[
            CallbackSubscription(event_type="X", target_url="https://example.com"),
        ],
    )
    await configs.save(config)
    dispatcher = CallbackDispatcher(configs, executions, http=always_fails)
    results = await dispatcher.dispatch("t", "X", {})
    assert results[0].status == CallbackStatus.FAILED
    assert results[0].attempts == 2
