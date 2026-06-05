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
"""Regression tests for callback dispatcher fixes (#190, #194)."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.callbacks.dispatcher import CallbackDispatcher
from pyfly.callbacks.models import AuthorizedDomain, CallbackConfig, CallbackStatus, CallbackSubscription
from pyfly.callbacks.repository import (
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)


@pytest.mark.asyncio
async def test_unauthorized_domain_is_blocked() -> None:
    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()
    calls: list[str] = []

    async def fake_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> int:
        calls.append(url)
        return 200

    config = CallbackConfig(
        tenant_id="t1",
        name="orders",
        authorized_domains=[AuthorizedDomain(domain="trusted.example.com")],
        subscriptions=[CallbackSubscription(event_type="E", target_url="https://evil.example.org/x")],
    )
    await configs.save(config)

    dispatcher = CallbackDispatcher(configs, executions, http=fake_post)
    [result] = await dispatcher.dispatch("t1", "E", {"a": 1})

    assert result.status == CallbackStatus.FAILED  # audit #190
    assert "authorized" in (result.last_error or "").lower()
    assert calls == []  # no HTTP request was made


@pytest.mark.asyncio
async def test_4xx_is_not_retried() -> None:
    configs = InMemoryCallbackConfigRepository()
    executions = InMemoryCallbackExecutionRepository()
    attempts = {"n": 0}

    async def fake_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> int:
        attempts["n"] += 1
        return 400  # permanent client error

    config = CallbackConfig(
        tenant_id="t1",
        name="orders",
        max_attempts=5,
        subscriptions=[CallbackSubscription(event_type="E", target_url="https://example.com/x")],
    )
    await configs.save(config)

    dispatcher = CallbackDispatcher(configs, executions, http=fake_post)
    [result] = await dispatcher.dispatch("t1", "E", {"a": 1})

    assert result.status == CallbackStatus.FAILED
    assert attempts["n"] == 1  # 400 not retried (audit #194)
