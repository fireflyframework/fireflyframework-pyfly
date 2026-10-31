# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Lightweight discovery; optional SQL composition is loaded only when explicitly enabled."""

from typing import Any

from pyfly.audit._dependencies import require_audit_sql
from pyfly.context.conditions import auto_configuration, conditional_on_property


@auto_configuration
@conditional_on_property("pyfly.audit.archive.enabled", having_value="true")
class AuditArchiveAutoConfiguration:
    def __new__(cls) -> Any:
        require_audit_sql("pyfly.audit.archive.enabled")
        from pyfly.audit._native_archive_auto_configuration import NativeAuditArchiveAutoConfiguration

        return NativeAuditArchiveAutoConfiguration()
