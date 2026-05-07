# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the EventGateway."""

from __future__ import annotations

import pytest

from pyfly.transactional.core.event_gateway import EventGateway


@pytest.mark.asyncio
async def test_dispatch_invokes_handlers() -> None:
    gw = EventGateway()
    captured: list = []

    async def handler(payload: object) -> str:
        captured.append(payload)
        return "ok"

    await gw.register("OrderPlaced", target="orderSaga", handler=handler)
    results = await gw.dispatch("OrderPlaced", {"id": 1})
    assert captured == [{"id": 1}]
    assert results == ["ok"]


@pytest.mark.asyncio
async def test_unregister() -> None:
    gw = EventGateway()

    async def handler(_: object) -> None: ...

    await gw.register("e", target="t", handler=handler)
    await gw.unregister("t")
    results = await gw.dispatch("e", None)
    assert results == []


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_dispatch() -> None:
    gw = EventGateway()

    async def fails(_: object) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    async def succeeds(_: object) -> str:
        return "ok"

    await gw.register("e", target="a", handler=fails)
    await gw.register("e", target="b", handler=succeeds)
    results = await gw.dispatch("e", None)
    # One handler raised, the other returned "ok".
    assert results == ["ok"]
