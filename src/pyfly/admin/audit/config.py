# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Explicit opt-in; sensitive reads never inherit the anonymous development baseline."""

from dataclasses import dataclass

from pyfly.core.config import config_properties


@config_properties(prefix="pyfly.admin.audit")
@dataclass
class AdminAuditProperties:
    enabled: bool = False
