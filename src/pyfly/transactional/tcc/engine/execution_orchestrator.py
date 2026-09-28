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
"""TCC execution orchestrator — three-phase Try/Confirm/Cancel coordinator.

Knowing what committed: every phase attempt runs inside :func:`pyfly.data.transaction.track_commits`.

- An attempt that failed or timed out after a unit of work of it committed (or with a commit whose outcome is
  unknown) is never retried: a retry would apply its writes twice. A timeout that fires while the attempt's
  ``COMMIT`` is in flight lets the commit finish (commits are shielded) and then fails the attempt.
- A participant whose TRY failed that way reserved something all the same: it takes part in the CANCEL
  phase with the participants whose TRY succeeded.
- A CONFIRM attempt that failed that way is a failed CONFIRM like any other (whether its work is complete
  cannot be told): the TCC fails, and every participant that tried is cancelled, the confirmed ones included.
- A caller that cancels the TCC (a timeout, a disconnect, shutdown) gets ``CancelledError`` once the CANCEL
  phase has run, to completion, for every participant that tried (the one whose TRY was cancelled after it
  committed included). Whichever phase the cancellation lands in, the CANCEL phase a failure started
  included: every CANCEL phase runs shielded, in a task of its own, so the cancellation never interrupts a
  participant's cancel nor skips the ones after it.

Participants run in the caller's task: inside the caller's ``@transactional`` their units of work join the
caller's unit, whose commit happens outside the TCC and is not seen (start a TCC outside a transaction, or
give the phase methods ``REQUIRES_NEW``). Only units of work the framework manages are seen
(``@transactional``, repositories, ``TransactionTemplate``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pyfly.data.transaction import track_commits
from pyfly.data.transaction.template import run_shielded
from pyfly.transactional.tcc.core.context import TccContext
from pyfly.transactional.tcc.core.phase import TccPhase
from pyfly.transactional.tcc.engine.participant_invoker import TccParticipantInvoker
from pyfly.transactional.tcc.registry.participant_definition import (
    ParticipantDefinition,
)
from pyfly.transactional.tcc.registry.tcc_definition import TccDefinition

logger = logging.getLogger(__name__)


class TccExecutionOrchestrator:
    """Three-phase coordinator for TCC transactions.

    Algorithm
    ---------
    1. **TRY phase** -- execute ``try_method`` for each participant in order.
       On failure go to CANCEL.
    2. **CONFIRM phase** (if all TRY succeeded) -- execute ``confirm_method``
       for each participant.  On failure go to CANCEL for remaining.
    3. **CANCEL phase** (if any failed) -- execute ``cancel_method`` for
       participants that completed TRY.

    Each phase respects per-participant timeout and retry.
    """

    def __init__(self, participant_invoker: TccParticipantInvoker) -> None:
        self._invoker = participant_invoker

    async def execute(
        self,
        tcc_def: TccDefinition,
        ctx: TccContext,
        input_data: Any = None,
    ) -> tuple[bool, str | None]:
        """Execute a TCC transaction through all three phases.

        Returns
        -------
        tuple[bool, str | None]
            ``(success, failed_participant_id)`` — ``True`` with ``None`` on
            success, ``False`` with the id of the participant that failed on
            failure.
        """
        participants = list(tcc_def.participants.values())
        bean = tcc_def.bean
        tried_ids: list[str] = []

        # ── TRY phase ────────────────────────────────────────────
        ctx.set_phase(TccPhase.TRY)
        failed_participant_id: str | None = None

        for p_def in participants:
            with track_commits() as commits:
                try:
                    result = await self._invoke_with_retry_and_timeout(
                        self._invoker.invoke_try,
                        p_def,
                        bean,
                        ctx,
                        input_data,
                        phase_attr="__pyfly_try_method__",
                        tcc_def=tcc_def,
                    )
                except asyncio.CancelledError:
                    # The caller cancelled the TCC: release what the participants reserved, then propagate.
                    if commits.may_have_committed:
                        tried_ids.append(p_def.id)
                    await self._cancel_on_cancellation(tried_ids, tcc_def, bean, ctx)
                    raise
                except Exception as exc:
                    ctx.record_participant_error(p_def.id, TccPhase.TRY, exc)
                    # A TRY that failed after it committed (its COMMIT landed as it timed out) reserved all the
                    # same: it is cancelled like a participant that tried.
                    committed = commits.may_have_committed
                    if p_def.optional:
                        logger.debug(
                            "Optional participant '%s' TRY failed (skipped): %s",
                            p_def.id,
                            exc,
                        )
                        if committed and await self._run_cancel_phase([p_def.id], tcc_def, bean, ctx):
                            # The caller cancelled the TCC meanwhile: release what the others reserved too.
                            await self._cancel_on_cancellation(tried_ids, tcc_def, bean, ctx)
                            raise asyncio.CancelledError from None
                        continue
                    logger.debug(
                        "Participant '%s' TRY failed: %s",
                        p_def.id,
                        exc,
                    )
                    if committed:
                        tried_ids.append(p_def.id)
                    failed_participant_id = p_def.id
                    break
            ctx.set_try_result(p_def.id, result)
            ctx.set_participant_status(p_def.id, TccPhase.TRY)
            tried_ids.append(p_def.id)

        if failed_participant_id is not None:
            # ── CANCEL phase (TRY failure) ───────────────────────
            ctx.set_phase(TccPhase.CANCEL)
            if await self._run_cancel_phase(tried_ids, tcc_def, bean, ctx):
                raise asyncio.CancelledError  # the caller cancelled the TCC meanwhile; the phase ran first
            return (False, failed_participant_id)

        # ── CONFIRM phase ────────────────────────────────────────
        ctx.set_phase(TccPhase.CONFIRM)

        for p_def in participants:
            if p_def.id not in tried_ids:
                continue
            try:
                await self._invoke_with_retry_and_timeout(
                    self._invoker.invoke_confirm,
                    p_def,
                    bean,
                    ctx,
                    None,
                    phase_attr="__pyfly_confirm_method__",
                    tcc_def=tcc_def,
                )
                ctx.set_participant_status(p_def.id, TccPhase.CONFIRM)
            except asyncio.CancelledError:
                # Cancelled while confirming: as on a CONFIRM failure, every participant that tried is cancelled.
                await self._cancel_on_cancellation(tried_ids, tcc_def, bean, ctx)
                raise
            except Exception as exc:
                ctx.record_participant_error(p_def.id, TccPhase.CONFIRM, exc)
                logger.debug(
                    "Participant '%s' CONFIRM failed: %s",
                    p_def.id,
                    exc,
                )
                failed_participant_id = p_def.id
                break

        if failed_participant_id is not None:
            # ── CANCEL phase (CONFIRM failure) ───────────────────
            ctx.set_phase(TccPhase.CANCEL)
            if await self._run_cancel_phase(tried_ids, tcc_def, bean, ctx):
                raise asyncio.CancelledError  # the caller cancelled the TCC meanwhile; the phase ran first
            return (False, failed_participant_id)

        return (True, None)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _cancel_participants(
        self,
        tried_ids: list[str],
        tcc_def: TccDefinition,
        bean: Any,
        ctx: TccContext,
    ) -> None:
        """Cancel all participants that completed TRY, in reverse order."""
        for pid in reversed(tried_ids):
            p_def = tcc_def.participants[pid]
            if p_def.cancel_method is None:
                continue
            try:
                await self._invoke_with_retry_and_timeout(
                    self._invoker.invoke_cancel,
                    p_def,
                    bean,
                    ctx,
                    None,
                    phase_attr="__pyfly_cancel_method__",
                    tcc_def=tcc_def,
                )
                ctx.set_participant_status(pid, TccPhase.CANCEL)
            except Exception as exc:
                ctx.record_participant_error(pid, TccPhase.CANCEL, exc)
                logger.warning(
                    "Participant '%s' CANCEL failed: %s",
                    pid,
                    exc,
                )

    async def _run_cancel_phase(
        self,
        tried_ids: list[str],
        tcc_def: TccDefinition,
        bean: Any,
        ctx: TccContext,
    ) -> bool:
        """Cancel *tried_ids* to completion whatever happens to the calling task, in a task of its own.

        Returns whether the caller was cancelled meanwhile: the caller raises ``CancelledError`` then.
        """
        _result, error, cancelled = await run_shielded(self._cancel_participants(tried_ids, tcc_def, bean, ctx))
        if error is not None:
            logger.warning("TCC CANCEL phase raised: %s", error)
        return cancelled

    async def _cancel_on_cancellation(
        self,
        tried_ids: list[str],
        tcc_def: TccDefinition,
        bean: Any,
        ctx: TccContext,
    ) -> None:
        """Run the CANCEL phase for *tried_ids* to completion although the caller is being cancelled."""
        ctx.set_phase(TccPhase.CANCEL)
        await self._run_cancel_phase(tried_ids, tcc_def, bean, ctx)

    async def _invoke_with_retry_and_timeout(
        self,
        invoke_fn: Any,
        p_def: ParticipantDefinition,
        bean: Any,
        ctx: TccContext,
        input_data: Any,
        *,
        phase_attr: str,
        tcc_def: TccDefinition | None = None,
    ) -> Any:
        """Invoke a participant phase method with retry and timeout.

        Reads retry/timeout/backoff from the method's ``__pyfly_*_method__``
        metadata, falling back to the class-level ``@tcc`` config when a method
        declares none. ``retry=N`` means N retries → N+1 total attempts, matching
        the Java engine (audit #56). Accumulates phase latency onto the context
        for result reporting (audit #57).
        """
        method = self._get_phase_method(p_def, phase_attr)
        meta = getattr(method, phase_attr, {}) if method is not None else {}

        method_retry = int(meta.get("retry", 0) or 0)
        if method_retry <= 0 and tcc_def is not None and getattr(tcc_def, "retry_enabled", False):
            method_retry = getattr(tcc_def, "max_retries", 0)
        total_attempts = method_retry + 1  # retry=N → N+1 attempts

        timeout_ms = meta.get("timeout_ms", 0) or p_def.timeout_ms
        if not timeout_ms and tcc_def is not None:
            timeout_ms = getattr(tcc_def, "timeout_ms", 0)
        backoff_ms = float(meta.get("backoff_ms", 0) or (getattr(tcc_def, "backoff_ms", 0) if tcc_def else 0))

        started = time.perf_counter()
        try:
            for attempt in range(1, total_attempts + 1):
                with track_commits() as commits:
                    try:
                        coro = self._build_coro(invoke_fn, p_def, bean, ctx, input_data)

                        if timeout_ms > 0:
                            async with asyncio.timeout(timeout_ms / 1000.0):
                                return await coro
                        return await coro

                    except Exception:
                        # An attempt that committed (its COMMIT landed as it timed out, say) is never retried.
                        if attempt < total_attempts and not commits.may_have_committed:
                            if backoff_ms > 0:
                                await asyncio.sleep(backoff_ms / 1000.0)
                            backoff_ms *= 2
                        else:
                            raise

            # Should never reach here, but satisfy type checker.
            raise RuntimeError("Unreachable")  # pragma: no cover
        finally:
            ctx.add_participant_latency(p_def.id, (time.perf_counter() - started) * 1000.0)

    @staticmethod
    def _get_phase_method(
        p_def: ParticipantDefinition,
        phase_attr: str,
    ) -> Any | None:
        """Return the method object for the given phase attribute."""
        if phase_attr == "__pyfly_try_method__":
            return p_def.try_method
        if phase_attr == "__pyfly_confirm_method__":
            return p_def.confirm_method
        if phase_attr == "__pyfly_cancel_method__":
            return p_def.cancel_method
        return None

    @staticmethod
    async def _build_coro(
        invoke_fn: Any,
        p_def: ParticipantDefinition,
        bean: Any,
        ctx: TccContext,
        input_data: Any,
    ) -> Any:
        """Build the coroutine for the invoke function.

        Handles the difference between invoke_try (4 args) and
        invoke_confirm/invoke_cancel (3 args).
        """
        import inspect as _inspect

        sig = _inspect.signature(invoke_fn)
        params = list(sig.parameters.keys())

        # invoke_try has input_data, invoke_confirm/cancel do not.
        if "input_data" in params:
            return await invoke_fn(p_def, bean, ctx, input_data)
        return await invoke_fn(p_def, bean, ctx)
