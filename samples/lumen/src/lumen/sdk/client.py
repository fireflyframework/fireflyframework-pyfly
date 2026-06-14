# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Typed HTTP client for the Lumen wallet service.

The client takes any ``httpx.AsyncClient`` so callers can configure
auth, retries, transport, and instrumentation centrally.
"""

from __future__ import annotations

from typing import Any

import httpx

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto


class LumenClient:
    """Async HTTP client for the Lumen REST API."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def open_wallet(self, request: OpenWalletRequest) -> str:
        response = await self._http.post("/api/v1/wallets", json=request.model_dump(mode="json"))
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return str(payload["wallet_id"])

    async def deposit(self, wallet_id: str, request: DepositRequest) -> int:
        response = await self._http.post(f"/api/v1/wallets/{wallet_id}/deposit", json=request.model_dump())
        response.raise_for_status()
        return int(response.json()["balance_minor"])

    async def withdraw(self, wallet_id: str, request: DepositRequest) -> int:
        response = await self._http.post(f"/api/v1/wallets/{wallet_id}/withdraw", json=request.model_dump())
        response.raise_for_status()
        return int(response.json()["balance_minor"])

    async def get_wallet(self, wallet_id: str) -> WalletDto | None:
        response = await self._http.get(f"/api/v1/wallets/{wallet_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return WalletDto.model_validate(response.json())

    async def get_balance(self, wallet_id: str) -> BalanceDto | None:
        response = await self._http.get(f"/api/v1/wallets/{wallet_id}/balance")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return BalanceDto.model_validate(response.json())
