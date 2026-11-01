# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Bounded immutable cursor snapshots, without invented totals or anchoring."""

from typing import Literal

from pyfly.audit.models import AuditHead, AuditScope, Count, Fact, JournalEntry


class AdminAuditPage(Fact):
    scope: AuditScope
    head: AuditHead
    entries: tuple[JournalEntry, ...]
    snapshot_through_sequence: Count
    next_after_sequence: Count
    has_more: bool
    can_verify: bool = False
    verification: Literal["not_verified"] = "not_verified"
    retention: Literal["journal_adapter"] = "journal_adapter"


class AdminAuditRange(Fact):
    scope: AuditScope
    first_sequence: Count
    last_sequence: Count
    previous_hash: str
    terminal_hash: str
    verification: Literal["range_verified_unanchored"] = "range_verified_unanchored"
