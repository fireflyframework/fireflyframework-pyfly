# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``TransferRequest`` — the input payload for the money-transfer saga.

A small immutable value object describing *move ``amount`` minor units of
``currency`` from ``source_wallet_id`` to ``destination_wallet_id``*. It is
handed to :meth:`SagaEngine.execute` as ``input_data`` and injected into the
saga steps via ``Annotated[TransferRequest, Input]``.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency


@dataclass(frozen=True)
class TransferRequest:
    """Describe a single wallet-to-wallet transfer.

    ``amount`` is in minor units (cents), matching :class:`Money`.
    """

    source_wallet_id: str
    destination_wallet_id: str
    amount: int
    currency: Currency
