# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`PostgresEventBus` — identifier validation + protocol shape.

The connection-bound paths (publish, drain) require a real Postgres and
are exercised end-to-end by flydesk-idp's docker-based smoke test.
"""

from __future__ import annotations

import pytest

from pyfly.eda.adapters.postgres import (
    PostgresEventBus,
    _group_lock_key,
    _normalise_dsn,
    _quote_ident,
)
from pyfly.eda.ports.outbound import EventPublisher


class TestPostgresEventBus:
    def test_protocol_compliance(self) -> None:
        bus = PostgresEventBus(dsn="postgresql://x/y", channel="pyfly_eda")
        assert isinstance(bus, EventPublisher)

    def test_channel_identifier_validated(self) -> None:
        # Reject anything that could let SQL inject through NOTIFY.
        with pytest.raises(ValueError):
            PostgresEventBus(dsn="postgresql://x/y", channel="bad channel")
        with pytest.raises(ValueError):
            PostgresEventBus(dsn="postgresql://x/y", channel="x;DROP TABLE")

    def test_valid_identifier_accepted(self) -> None:
        assert _quote_ident("pyfly_eda") == "pyfly_eda"
        assert _quote_ident("Pyfly_Eda_123") == "Pyfly_Eda_123"

    def test_destinations_default_to_all(self) -> None:
        bus = PostgresEventBus(dsn="postgresql://x/y")
        assert bus._destinations is None

    def test_destinations_filter_preserved(self) -> None:
        bus = PostgresEventBus(
            dsn="postgresql://x/y",
            destinations=["flydesk.idp.jobs", "flydesk.idp.completions"],
        )
        assert bus._destinations == [
            "flydesk.idp.jobs",
            "flydesk.idp.completions",
        ]

    def test_normalise_dsn_strips_dialect_markers(self) -> None:
        assert (
            _normalise_dsn("postgresql+asyncpg://u:p@h:5432/db")
            == "postgresql://u:p@h:5432/db"
        )
        assert (
            _normalise_dsn("postgresql+psycopg://u:p@h/db")
            == "postgresql://u:p@h/db"
        )
        assert _normalise_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"

    def test_normalise_dsn_applied_in_constructor(self) -> None:
        bus = PostgresEventBus(
            dsn="postgresql+asyncpg://idp:idp@pg:5432/flydesk_idp",
            listen_dsn="postgresql+asyncpg://idp:idp@pg:5432/flydesk_idp",
        )
        assert bus._dsn == "postgresql://idp:idp@pg:5432/flydesk_idp"
        assert bus._listen_dsn == "postgresql://idp:idp@pg:5432/flydesk_idp"


class TestGroupLockKey:
    """``_group_lock_key`` -- deterministic, signed-64-bit advisory lock."""

    def test_same_group_yields_same_key(self) -> None:
        assert _group_lock_key("flydocs-workers") == _group_lock_key("flydocs-workers")

    def test_different_groups_yield_different_keys(self) -> None:
        assert _group_lock_key("flydocs-workers") != _group_lock_key("flydocs-bbox-workers")

    def test_fits_in_signed_bigint(self) -> None:
        """Postgres advisory locks take ``bigint`` (signed 64-bit)."""
        for group in ("a", "flydocs-workers", "very-long-group-name-with-suffix"):
            key = _group_lock_key(group)
            assert -(2**63) <= key < 2**63
