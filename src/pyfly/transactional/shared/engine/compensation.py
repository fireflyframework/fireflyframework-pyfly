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
"""Compensation error handlers for the transactional engine.

Four implementations of :class:`CompensationErrorHandlerPort`:

* :class:`FailFastErrorHandler` -- re-raises immediately
* :class:`LogAndContinueErrorHandler` -- logs and continues
* :class:`RetryWithBackoffErrorHandler` -- retries with exponential backoff
* :class:`CompositeCompensationErrorHandler` -- primary with fallback

Plus a :class:`CompensationErrorHandlerFactory` for creating handlers by name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from pyfly.transactional.shared.ports.outbound import CompensationErrorHandlerPort

logger = logging.getLogger(__name__)


class FailFastErrorHandler:
    """Re-raises the compensation error immediately, stopping further compensations."""

    async def handle(self, saga_name: str, step_id: str, error: Exception, ctx: Any) -> None:
        raise error


class LogAndContinueErrorHandler:
    """Logs the compensation error and continues with the next compensation."""

    async def handle(self, saga_name: str, step_id: str, error: Exception, ctx: Any) -> None:
        logger.error(
            "Compensation failed for saga '%s', step '%s': %s",
            saga_name,
            step_id,
            error,
        )


class RetryWithBackoffErrorHandler:
    """Retries the compensation with exponential backoff.

    If a ``compensation_fn`` callable is present in *ctx* (under the key
    ``"compensation_fn"``), retries will invoke that callable.  Otherwise
    the handler simply re-raises the original error after exhausting all
    retries.

    Args:
        max_retries: Maximum number of retry attempts.
        backoff_ms: Initial backoff delay in milliseconds.
        backoff_multiplier: Multiplier applied to the delay after each retry.
    """

    def __init__(
        self,
        max_retries: int = 3,
        backoff_ms: int = 1000,
        backoff_multiplier: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_ms = backoff_ms
        self.backoff_multiplier = backoff_multiplier

    async def handle(self, saga_name: str, step_id: str, error: Exception, ctx: Any) -> None:
        compensation_fn = ctx.get("compensation_fn") if isinstance(ctx, dict) else None

        delay_s = self.backoff_ms / 1000.0

        for attempt in range(self.max_retries):
            await asyncio.sleep(delay_s)

            if compensation_fn is not None:
                try:
                    await compensation_fn()
                    return  # Compensation succeeded on retry
                except Exception as retry_error:  # noqa: BLE001
                    if attempt == self.max_retries - 1:
                        raise retry_error from error
                    delay_s *= self.backoff_multiplier
            else:
                # No compensation_fn — after all retries, re-raise the original error
                if attempt == self.max_retries - 1:
                    raise error
                delay_s *= self.backoff_multiplier


class CompositeCompensationErrorHandler:
    """Tries a primary handler first; falls back to a secondary handler on failure.

    Args:
        primary: The first handler to attempt.
        fallback: The handler invoked when *primary* raises.
    """

    def __init__(
        self,
        primary: CompensationErrorHandlerPort,
        fallback: CompensationErrorHandlerPort,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def handle(self, saga_name: str, step_id: str, error: Exception, ctx: Any) -> None:
        try:
            await self._primary.handle(saga_name, step_id, error, ctx)
        except Exception:  # noqa: BLE001
            await self._fallback.handle(saga_name, step_id, error, ctx)


class CompensationErrorHandlerFactory:
    """Factory for creating compensation error handlers by type name."""

    @staticmethod
    def create(handler_type: str, **kwargs: Any) -> CompensationErrorHandlerPort:
        """Create a compensation error handler by type name.

        Args:
            handler_type: One of ``"fail_fast"``, ``"log_and_continue"``,
                ``"retry_with_backoff"``.
            **kwargs: Handler-specific configuration passed to the constructor.

        Returns:
            A handler instance satisfying :class:`CompensationErrorHandlerPort`.

        Raises:
            ValueError: If *handler_type* is not recognised.
        """
        handlers: dict[str, type] = {
            "fail_fast": FailFastErrorHandler,
            "log_and_continue": LogAndContinueErrorHandler,
            "retry_with_backoff": RetryWithBackoffErrorHandler,
        }

        handler_cls = handlers.get(handler_type)
        if handler_cls is None:
            raise ValueError(
                f"Unknown compensation error handler type: '{handler_type}'. "
                f"Available types: {', '.join(sorted(handlers))}"
            )

        return cast(CompensationErrorHandlerPort, handler_cls(**kwargs))
