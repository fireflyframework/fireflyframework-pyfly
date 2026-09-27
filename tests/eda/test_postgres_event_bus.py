# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`PostgresEventBus` — identifier validation, URLs and protocol shape.

The connection-bound paths (publish, claim, LISTEN) run against a real PostgreSQL in
``tests/integration/test_eda_buses_integration.py`` and on every backend in
``tests/integration/test_eda_outbox_matrix.py``.
"""

from __future__ import annotations

import pytest

from pyfly.eda.adapters.database import DatabaseEventBus
from pyfly.eda.adapters.postgres import (
    PostgresEventBus,
    _normalise_dsn,
    _quote_ident,
    _sqlalchemy_url,
)
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.kernel.lifecycle import CONSUMER_PHASE


class TestPostgresEventBus:
    def test_protocol_compliance(self) -> None:
        bus = PostgresEventBus(dsn="postgresql://x/y", channel="pyfly_eda")
        assert isinstance(bus, EventPublisher)
        assert isinstance(bus, DatabaseEventBus)

    def test_it_is_a_consumer_that_joins_transactions(self) -> None:
        bus = PostgresEventBus(dsn="postgresql://x/y")
        assert bus.phase == CONSUMER_PHASE
        assert bus.joins_transactions is True
        assert bus.manages_listener_errors is True

    def test_channel_identifier_validated(self) -> None:
        # Reject anything that could let SQL inject through LISTEN/NOTIFY.
        with pytest.raises(ValueError):
            PostgresEventBus(dsn="postgresql://x/y", channel="bad channel")
        with pytest.raises(ValueError):
            PostgresEventBus(dsn="postgresql://x/y", channel="x;DROP TABLE")

    def test_valid_identifier_accepted(self) -> None:
        assert _quote_ident("pyfly_eda") == "pyfly_eda"
        assert _quote_ident("Pyfly_Eda_123") == "Pyfly_Eda_123"

    def test_destinations_default_to_all(self) -> None:
        bus = PostgresEventBus(dsn="postgresql://x/y")
        assert bus.destinations is None

    def test_destinations_filter_preserved(self) -> None:
        bus = PostgresEventBus(
            dsn="postgresql://x/y",
            destinations=["flydesk.idp.jobs", "flydesk.idp.completions"],
        )
        assert bus.destinations == ["flydesk.idp.jobs", "flydesk.idp.completions"]

    def test_normalise_dsn_strips_dialect_markers(self) -> None:
        assert _normalise_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
        assert _normalise_dsn("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
        assert _normalise_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"

    def test_a_plain_dsn_becomes_an_asyncpg_url(self) -> None:
        """SQLAlchemy 2.1 resolves a driverless ``postgresql://`` URL to psycopg."""
        assert _sqlalchemy_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
        assert _sqlalchemy_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
        assert _sqlalchemy_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"

    def test_a_dsn_and_a_datasource_are_exclusive(self) -> None:
        with pytest.raises(ValueError, match="dsn or a datasource"):
            PostgresEventBus(dsn="postgresql://x/y", datasource="primary")

    def test_the_old_keyword_arguments_are_accepted(self) -> None:
        bus = PostgresEventBus(
            dsn="postgresql://x/y",
            listen_dsn="postgresql://x/y",
            channel="pyfly_eda",
            destinations=["pyfly.events"],
            group="workers",
            poll_interval_s=1.5,
            auto_create_tables=False,
        )
        assert bus.group == "workers"
        assert bus.relay.poll_interval == 1.5
        assert bus.running is False
