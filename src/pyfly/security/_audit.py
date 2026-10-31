# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Static native security decision hints; the host resolver supplies every actor and scope."""

from typing import Literal

from pyfly.audit.recording_binding import AuditRecordingSupport
from pyfly.audit.recording_context import AuditOperation


class SecurityAuditSupport(AuditRecordingSupport):
    __pyfly_audit_module__ = "security"

    async def _record_security(
        self,
        action: Literal["authenticate", "authorize", "session_restore"],
        resource_key: str,
        outcome: Literal["admitted", "denied"],
    ) -> None:
        recorder = self._audit_recorder("security")
        if recorder is not None:
            await recorder.observation(
                AuditOperation(
                    module="security",
                    action=action,
                    resource_kind="http-authorization" if action == "authorize" else "authentication",
                    resource_key=resource_key,
                ),
                outcome=outcome,
            )
