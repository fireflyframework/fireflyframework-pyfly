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
"""Tests for shared observability event adapters."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from pyfly.transactional.shared.observability.events import (
    CompositeEventsAdapter,
    LoggerEventsAdapter,
)
from pyfly.transactional.shared.ports.outbound import TransactionalEventsPort

pytestmark = pytest.mark.anyio
anyio_backend = "asyncio"


# ---------------------------------------------------------------------------
# LoggerEventsAdapter
# ---------------------------------------------------------------------------


class TestLoggerEventsAdapter:
    """LoggerEventsAdapter logs transactional lifecycle events."""

    @pytest.fixture
    def adapter(self) -> LoggerEventsAdapter:
        return LoggerEventsAdapter()

    # -- on_start -----------------------------------------------------------

    async def test_on_start_logs_at_info_level(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_start("order-saga", "corr-123")

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.INFO
        assert "order-saga" in record.message
        assert "corr-123" in record.message

    async def test_on_start_message_format(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_start("order-saga", "corr-abc")

        assert "Saga 'order-saga' started [correlation_id=corr-abc]" in caplog.records[0].message

    # -- on_step_success ----------------------------------------------------

    async def test_on_step_success_logs_at_info_level(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_step_success("order-saga", "corr-123", "reserve-stock", attempts=2, latency_ms=42.567)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.INFO
        assert "reserve-stock" in record.message
        assert "order-saga" in record.message

    async def test_on_step_success_message_format(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_step_success("order-saga", "corr-123", "reserve-stock", attempts=2, latency_ms=42.567)

        expected = "Step 'reserve-stock' succeeded [saga=order-saga, attempts=2, latency=42.6ms]"
        assert expected in caplog.records[0].message

    # -- on_step_failed -----------------------------------------------------

    async def test_on_step_failed_logs_at_warning_level(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        error = RuntimeError("network timeout")

        with caplog.at_level(logging.WARNING, logger="pyfly.transactional.events"):
            await adapter.on_step_failed(
                "order-saga",
                "corr-123",
                "charge-payment",
                error=error,
                attempts=3,
                latency_ms=1500.0,
            )

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        assert "charge-payment" in record.message
        assert "network timeout" in record.message

    async def test_on_step_failed_message_format(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        error = ValueError("bad amount")

        with caplog.at_level(logging.WARNING, logger="pyfly.transactional.events"):
            await adapter.on_step_failed(
                "order-saga",
                "corr-456",
                "charge-payment",
                error=error,
                attempts=1,
                latency_ms=99.99,
            )

        expected = "Step 'charge-payment' failed [saga=order-saga, attempts=1, latency=100.0ms]: bad amount"
        assert expected in caplog.records[0].message

    # -- on_compensated -----------------------------------------------------

    async def test_on_compensated_logs_at_info_when_no_error(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_compensated("order-saga", "corr-123", "reserve-stock", error=None)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.INFO
        assert "reserve-stock" in record.message
        assert "compensated" in record.message

    async def test_on_compensated_message_format_no_error(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_compensated("order-saga", "corr-123", "reserve-stock", error=None)

        expected = "Step 'reserve-stock' compensated [saga=order-saga]"
        assert expected in caplog.records[0].message

    async def test_on_compensated_logs_at_warning_when_error(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        error = RuntimeError("compensation failed")

        with caplog.at_level(logging.WARNING, logger="pyfly.transactional.events"):
            await adapter.on_compensated("order-saga", "corr-123", "reserve-stock", error=error)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        assert "reserve-stock" in record.message
        assert "compensation failed" in record.message

    async def test_on_compensated_message_format_with_error(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        error = RuntimeError("compensation failed")

        with caplog.at_level(logging.WARNING, logger="pyfly.transactional.events"):
            await adapter.on_compensated("order-saga", "corr-123", "reserve-stock", error=error)

        expected = "Step 'reserve-stock' compensated [saga=order-saga]: compensation failed"
        assert expected in caplog.records[0].message

    # -- on_completed -------------------------------------------------------

    async def test_on_completed_logs_at_info_level(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_completed("order-saga", "corr-123", success=True)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.INFO
        assert "order-saga" in record.message
        assert "corr-123" in record.message

    async def test_on_completed_message_format(
        self, adapter: LoggerEventsAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            await adapter.on_completed("order-saga", "corr-789", success=False)

        expected = "Saga 'order-saga' completed [correlation_id=corr-789, success=False]"
        assert expected in caplog.records[0].message

    # -- protocol compliance ------------------------------------------------

    def test_implements_transactional_events_port(self) -> None:
        assert isinstance(LoggerEventsAdapter(), TransactionalEventsPort)


# ---------------------------------------------------------------------------
# CompositeEventsAdapter
# ---------------------------------------------------------------------------


def _mock_events_adapter() -> AsyncMock:
    """Create an ``AsyncMock`` that satisfies ``TransactionalEventsPort``."""
    mock = AsyncMock(spec=TransactionalEventsPort)
    return mock


class TestCompositeEventsAdapter:
    """CompositeEventsAdapter broadcasts to multiple adapters."""

    # -- broadcasting -------------------------------------------------------

    async def test_broadcasts_on_start_to_all_adapters(self) -> None:
        a1, a2 = _mock_events_adapter(), _mock_events_adapter()
        composite = CompositeEventsAdapter(a1, a2)

        await composite.on_start("order-saga", "corr-1")

        a1.on_start.assert_awaited_once_with("order-saga", "corr-1")
        a2.on_start.assert_awaited_once_with("order-saga", "corr-1")

    async def test_broadcasts_on_step_success_to_all_adapters(self) -> None:
        a1, a2 = _mock_events_adapter(), _mock_events_adapter()
        composite = CompositeEventsAdapter(a1, a2)

        await composite.on_step_success("order-saga", "corr-1", "step-1", attempts=1, latency_ms=10.0)

        a1.on_step_success.assert_awaited_once_with("order-saga", "corr-1", "step-1", attempts=1, latency_ms=10.0)
        a2.on_step_success.assert_awaited_once_with("order-saga", "corr-1", "step-1", attempts=1, latency_ms=10.0)

    async def test_broadcasts_on_step_failed_to_all_adapters(self) -> None:
        a1, a2 = _mock_events_adapter(), _mock_events_adapter()
        composite = CompositeEventsAdapter(a1, a2)
        error = RuntimeError("boom")

        await composite.on_step_failed("order-saga", "corr-1", "step-1", error=error, attempts=2, latency_ms=5.0)

        a1.on_step_failed.assert_awaited_once_with(
            "order-saga", "corr-1", "step-1", error=error, attempts=2, latency_ms=5.0
        )
        a2.on_step_failed.assert_awaited_once_with(
            "order-saga", "corr-1", "step-1", error=error, attempts=2, latency_ms=5.0
        )

    async def test_broadcasts_on_compensated_to_all_adapters(self) -> None:
        a1, a2 = _mock_events_adapter(), _mock_events_adapter()
        composite = CompositeEventsAdapter(a1, a2)

        await composite.on_compensated("order-saga", "corr-1", "step-1", error=None)

        a1.on_compensated.assert_awaited_once_with("order-saga", "corr-1", "step-1", error=None)
        a2.on_compensated.assert_awaited_once_with("order-saga", "corr-1", "step-1", error=None)

    async def test_broadcasts_on_completed_to_all_adapters(self) -> None:
        a1, a2 = _mock_events_adapter(), _mock_events_adapter()
        composite = CompositeEventsAdapter(a1, a2)

        await composite.on_completed("order-saga", "corr-1", success=True)

        a1.on_completed.assert_awaited_once_with("order-saga", "corr-1", success=True)
        a2.on_completed.assert_awaited_once_with("order-saga", "corr-1", success=True)

    # -- error handling -----------------------------------------------------

    async def test_continues_when_adapter_fails(self) -> None:
        a1 = _mock_events_adapter()
        a1.on_start.side_effect = RuntimeError("adapter-1 exploded")
        a2 = _mock_events_adapter()

        composite = CompositeEventsAdapter(a1, a2)
        await composite.on_start("order-saga", "corr-1")

        # a2 should still have been called despite a1 failure
        a2.on_start.assert_awaited_once_with("order-saga", "corr-1")

    async def test_logs_adapter_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        a1 = _mock_events_adapter()
        a1.on_start.side_effect = RuntimeError("adapter-1 exploded")
        a2 = _mock_events_adapter()

        composite = CompositeEventsAdapter(a1, a2)

        with caplog.at_level(logging.ERROR, logger="pyfly.transactional.events"):
            await composite.on_start("order-saga", "corr-1")

        # The error message appears in the traceback (exc_info), not the log
        # message itself.  caplog.text includes the formatted traceback.
        assert "adapter-1 exploded" in caplog.text

    async def test_handles_all_adapters_failing(self) -> None:
        a1 = _mock_events_adapter()
        a1.on_completed.side_effect = RuntimeError("a1 boom")
        a2 = _mock_events_adapter()
        a2.on_completed.side_effect = ValueError("a2 boom")

        composite = CompositeEventsAdapter(a1, a2)

        # Should not raise even if all adapters fail
        await composite.on_completed("order-saga", "corr-1", success=False)

    # -- edge cases ---------------------------------------------------------

    async def test_empty_composite_does_not_fail(self) -> None:
        composite = CompositeEventsAdapter()
        await composite.on_start("order-saga", "corr-1")

    async def test_single_adapter_works(self) -> None:
        a1 = _mock_events_adapter()
        composite = CompositeEventsAdapter(a1)

        await composite.on_completed("order-saga", "corr-1", success=True)

        a1.on_completed.assert_awaited_once_with("order-saga", "corr-1", success=True)

    # -- protocol compliance ------------------------------------------------

    def test_implements_transactional_events_port(self) -> None:
        assert isinstance(CompositeEventsAdapter(), TransactionalEventsPort)
