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
"""Tests for shared compensation error handlers."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from pyfly.transactional.shared.engine.compensation import (
    CompensationErrorHandlerFactory,
    CompositeCompensationErrorHandler,
    FailFastErrorHandler,
    LogAndContinueErrorHandler,
    RetryWithBackoffErrorHandler,
)
from pyfly.transactional.shared.ports.outbound import CompensationErrorHandlerPort

pytestmark = pytest.mark.anyio
anyio_backend = "asyncio"


# ---------------------------------------------------------------------------
# FailFastErrorHandler
# ---------------------------------------------------------------------------


class TestFailFastErrorHandler:
    """FailFastErrorHandler re-raises the original error immediately."""

    async def test_handle_raises_original_error(self) -> None:
        handler = FailFastErrorHandler()
        error = RuntimeError("compensation exploded")

        with pytest.raises(RuntimeError, match="compensation exploded"):
            await handler.handle("order-saga", "step-1", error, {})

    def test_implements_protocol(self) -> None:
        assert isinstance(FailFastErrorHandler(), CompensationErrorHandlerPort)


# ---------------------------------------------------------------------------
# LogAndContinueErrorHandler
# ---------------------------------------------------------------------------


class TestLogAndContinueErrorHandler:
    """LogAndContinueErrorHandler logs the error and does NOT raise."""

    async def test_handle_does_not_raise(self) -> None:
        handler = LogAndContinueErrorHandler()
        error = RuntimeError("compensation failed")

        # Must not raise
        await handler.handle("order-saga", "step-2", error, {"key": "value"})

    async def test_handle_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = LogAndContinueErrorHandler()
        error = RuntimeError("oops")

        with caplog.at_level(logging.ERROR):
            await handler.handle("order-saga", "step-2", error, {})

        assert "order-saga" in caplog.text
        assert "step-2" in caplog.text
        assert "oops" in caplog.text

    def test_implements_protocol(self) -> None:
        assert isinstance(LogAndContinueErrorHandler(), CompensationErrorHandlerPort)


# ---------------------------------------------------------------------------
# RetryWithBackoffErrorHandler
# ---------------------------------------------------------------------------


class TestRetryWithBackoffErrorHandler:
    """RetryWithBackoffErrorHandler retries with exponential backoff."""

    async def test_raises_after_max_retries(self) -> None:
        handler = RetryWithBackoffErrorHandler(max_retries=3, backoff_ms=10, backoff_multiplier=2.0)
        error = RuntimeError("always fails")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(RuntimeError, match="always fails"):
                await handler.handle("order-saga", "step-3", error, {})

            # Should have slept 3 times (one per retry)
            assert mock_sleep.await_count == 3

    async def test_backoff_delays_are_exponential(self) -> None:
        handler = RetryWithBackoffErrorHandler(max_retries=3, backoff_ms=100, backoff_multiplier=2.0)
        error = RuntimeError("fails")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(RuntimeError):
                await handler.handle("order-saga", "step-3", error, {})

            delays = [call.args[0] for call in mock_sleep.call_args_list]
            assert delays == pytest.approx([0.1, 0.2, 0.4])

    async def test_succeeds_on_retry(self) -> None:
        """Compensation succeeds on the second retry via a mutable counter."""
        call_count = {"n": 0}

        class _RetryableError(Exception):
            pass

        async def _compensation_fn() -> None:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise _RetryableError("not yet")

        handler = RetryWithBackoffErrorHandler(max_retries=5, backoff_ms=10, backoff_multiplier=1.0)

        ctx = {"compensation_fn": _compensation_fn}
        error = _RetryableError("first failure")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handler.handle("order-saga", "step-x", error, ctx)

        # The compensation_fn was called 3 times (failed twice, succeeded once)
        assert call_count["n"] == 3

    async def test_default_configuration(self) -> None:
        handler = RetryWithBackoffErrorHandler()
        assert handler.max_retries == 3
        assert handler.backoff_ms == 1000
        assert handler.backoff_multiplier == 2.0

    def test_implements_protocol(self) -> None:
        assert isinstance(RetryWithBackoffErrorHandler(), CompensationErrorHandlerPort)


# ---------------------------------------------------------------------------
# CompositeCompensationErrorHandler
# ---------------------------------------------------------------------------


class TestCompositeCompensationErrorHandler:
    """CompositeCompensationErrorHandler chains primary -> fallback."""

    async def test_primary_succeeds_fallback_not_called(self) -> None:
        primary = AsyncMock()
        primary.handle = AsyncMock(return_value=None)
        fallback = AsyncMock()
        fallback.handle = AsyncMock(return_value=None)

        handler = CompositeCompensationErrorHandler(primary=primary, fallback=fallback)
        error = RuntimeError("test")

        await handler.handle("order-saga", "step-5", error, {})

        primary.handle.assert_awaited_once_with("order-saga", "step-5", error, {})
        fallback.handle.assert_not_awaited()

    async def test_primary_fails_fallback_succeeds(self) -> None:
        primary = AsyncMock()
        primary.handle = AsyncMock(side_effect=RuntimeError("primary boom"))
        fallback = AsyncMock()
        fallback.handle = AsyncMock(return_value=None)

        handler = CompositeCompensationErrorHandler(primary=primary, fallback=fallback)
        error = RuntimeError("original")

        await handler.handle("order-saga", "step-5", error, {})

        primary.handle.assert_awaited_once_with("order-saga", "step-5", error, {})
        fallback.handle.assert_awaited_once_with("order-saga", "step-5", error, {})

    async def test_both_fail_raises_fallback_error(self) -> None:
        primary = AsyncMock()
        primary.handle = AsyncMock(side_effect=RuntimeError("primary boom"))
        fallback = AsyncMock()
        fallback.handle = AsyncMock(side_effect=ValueError("fallback boom"))

        handler = CompositeCompensationErrorHandler(primary=primary, fallback=fallback)
        error = RuntimeError("original")

        with pytest.raises(ValueError, match="fallback boom"):
            await handler.handle("order-saga", "step-5", error, {})

    def test_implements_protocol(self) -> None:
        primary = LogAndContinueErrorHandler()
        fallback = FailFastErrorHandler()
        assert isinstance(
            CompositeCompensationErrorHandler(primary=primary, fallback=fallback),
            CompensationErrorHandlerPort,
        )


# ---------------------------------------------------------------------------
# CompensationErrorHandlerFactory
# ---------------------------------------------------------------------------


class TestCompensationErrorHandlerFactory:
    """Factory creates handlers by type name."""

    def test_create_fail_fast(self) -> None:
        handler = CompensationErrorHandlerFactory.create("fail_fast")
        assert isinstance(handler, FailFastErrorHandler)

    def test_create_log_and_continue(self) -> None:
        handler = CompensationErrorHandlerFactory.create("log_and_continue")
        assert isinstance(handler, LogAndContinueErrorHandler)

    def test_create_retry_with_backoff(self) -> None:
        handler = CompensationErrorHandlerFactory.create("retry_with_backoff", max_retries=2)
        assert isinstance(handler, RetryWithBackoffErrorHandler)
        assert handler.max_retries == 2

    def test_create_retry_with_backoff_custom_config(self) -> None:
        handler = CompensationErrorHandlerFactory.create(
            "retry_with_backoff",
            max_retries=5,
            backoff_ms=500,
            backoff_multiplier=1.5,
        )
        assert isinstance(handler, RetryWithBackoffErrorHandler)
        assert handler.max_retries == 5
        assert handler.backoff_ms == 500
        assert handler.backoff_multiplier == 1.5

    def test_create_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            CompensationErrorHandlerFactory.create("unknown_handler")
