# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Enabled SQL-backed composition fails explicitly without installing optional packages."""

import importlib.util


def require_audit_sql(feature: str) -> None:
    try:
        available = importlib.util.find_spec("sqlalchemy") is not None
    except ImportError:
        available = False
    if not available:
        raise RuntimeError(f"{feature} requires pyfly[data-relational]; missing: sqlalchemy") from None
