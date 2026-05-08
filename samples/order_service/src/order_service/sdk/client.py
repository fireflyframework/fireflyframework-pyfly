# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Typed HTTP client for the Orders Service.

Mirrors :code:`FireflyFramework.Samples.OrdersService.Sdk.OrdersServiceClient`.

The client takes any ``httpx.AsyncClient`` so callers can configure
auth, retries, transport, and instrumentation centrally.
"""

from __future__ import annotations

from typing import Any

import httpx

from order_service.interfaces.dtos.v1.order_dto import OrderDto
from order_service.interfaces.dtos.v1.place_order_request import PlaceOrderRequest


class OrdersServiceClient:
    """Async HTTP client for the Orders Service REST API."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def place_order(
        self,
        request: PlaceOrderRequest,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        response = await self._http.post(
            "/api/v1/orders",
            json=request.model_dump(),
            headers=headers or None,  # type: ignore[arg-type]
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return str(payload["order_id"])

    async def get_order(self, order_id: str) -> OrderDto | None:
        response = await self._http.get(f"/api/v1/orders/{order_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return OrderDto.model_validate(response.json())

    async def confirm_order(self, order_id: str) -> dict[str, str]:
        response = await self._http.post(f"/api/v1/orders/{order_id}/confirm")
        response.raise_for_status()
        return dict(response.json())
