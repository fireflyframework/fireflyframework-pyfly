# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Declared transport capabilities, not a network-readiness or delivery assertion."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmailProviderCapabilities:
    real_delivery: bool = False
    inline_cid: bool = False
    operation_lookup: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.real_delivery, self.inline_cid, self.operation_lookup)):
            raise ValueError("Email provider capabilities require explicit booleans")


def email_capabilities(provider: object) -> EmailProviderCapabilities:
    value = getattr(provider, "capabilities", None)
    return value if isinstance(value, EmailProviderCapabilities) else EmailProviderCapabilities()
