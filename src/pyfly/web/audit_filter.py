# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Optional native dispatch observations, without response-body or delivery claims."""

import asyncio
from typing import Any

from pyfly.audit.recording_binding import AuditRecordingSupport
from pyfly.audit.recording_context import AuditOperation
from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


class AuditWebFilter(AuditRecordingSupport, OncePerRequestFilter):
    __pyfly_order__ = HIGHEST_PRECEDENCE + 340
    __pyfly_audit_module__ = "web"

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        recorder = self._audit_recorder("web")
        if recorder is None:
            return await call_next(request)
        operation = AuditOperation(module="web", action="request", resource_kind="http-dispatch")
        await recorder.observation(operation, outcome="started")
        try:
            response = await call_next(request)
        except asyncio.CancelledError:
            await recorder.observation(operation, outcome="unknown")
            raise asyncio.CancelledError from None
        except Exception:
            await recorder.observation(operation, outcome="unknown")
            raise
        await recorder.observation(operation, outcome="observed")
        return response
