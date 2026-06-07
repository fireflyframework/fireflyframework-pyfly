# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``TransferService`` — the application entry point for money transfers.

Injects the framework-provided :class:`SagaEngine` (auto-configured by
``@enable_domain_stack`` + ``pyfly.transactional.enabled``) and runs the
``money-transfer`` saga by name. The returned :class:`SagaResult` is folded
into a small, JSON-friendly summary dict.
"""

from __future__ import annotations

from typing import Any

from lumen.core.services.transfers.money_transfer_saga import MONEY_TRANSFER_SAGA
from lumen.core.services.transfers.transfer_request import TransferRequest
from pyfly.container import service
from pyfly.transactional.saga.core.result import SagaResult
from pyfly.transactional.saga.engine.saga_engine import SagaEngine


@service
class TransferService:
    """Run the money-transfer saga and report the outcome."""

    def __init__(self, saga_engine: SagaEngine) -> None:
        self._saga_engine = saga_engine

    async def transfer(self, request: TransferRequest) -> dict[str, Any]:
        """Execute the transfer saga; return a summary of what happened.

        On success the destination balance is reported; on failure the failed
        step ids are reported and the engine has already compensated the debit,
        so both wallets are back to their original balances.
        """
        result: SagaResult = await self._saga_engine.execute(
            saga_name=MONEY_TRANSFER_SAGA,
            input_data=request,
        )

        if result.success:
            debit = result.result_of("debit-source")
            return {
                "status": "completed",
                "correlation_id": result.correlation_id,
                "source_balance": debit.balance,
                "destination_balance": result.result_of("credit-destination"),
            }

        return {
            "status": "failed",
            "correlation_id": result.correlation_id,
            "failed_steps": list(result.failed_steps().keys()),
            "compensated_steps": list(result.compensated_steps().keys()),
            "error": str(result.error),
        }
