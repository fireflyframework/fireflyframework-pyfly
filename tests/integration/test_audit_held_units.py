# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""External audit dispatch must not hide native held or repository read units."""

import pytest

from pyfly.data.transaction import Propagation, TransactionTemplate, resolve_manager
from pyfly.data.transaction.template import AutoUnit
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")


@pytest.mark.parametrize("mode", ["write", "read", "suspended", "auto_read"])
async def test_held_data_unit_guard_sees_all_native_scopes(relational_backend: RelationalBackend, mode: str):
    from pyfly.audit import _infrastructure

    assert hasattr(_infrastructure, "has_active_data_units")
    guard = _infrastructure.has_active_data_units
    manager = resolve_manager(relational_backend.create_engine())
    assert not guard()
    if mode == "auto_read":
        async with AutoUnit(manager, read_only=True, autocommit=True):
            assert guard()
    else:
        async with TransactionTemplate(manager).transaction(read_only=mode == "read"):
            assert guard()
            if mode == "suspended":
                async with TransactionTemplate(manager).transaction(propagation=Propagation.NOT_SUPPORTED):
                    assert guard()
    assert not guard()
