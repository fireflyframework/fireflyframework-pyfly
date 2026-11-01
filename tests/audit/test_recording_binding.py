# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from pyfly.audit import AuditError
from pyfly.audit.recording_binding import AuditModulePostProcessor, AuditRecordingSupport


def test_native_binding_is_lazy_and_cannot_rebind_shared_instances():
    calls = []
    target = AuditRecordingSupport()

    def factory():
        calls.append(True)
        return object()

    processor = AuditModulePostProcessor(factory)
    assert processor.after_init(target, "fixture") is target
    assert calls == []
    with pytest.raises(AuditError, match="another application"):
        AuditModulePostProcessor(lambda: object()).after_init(target, "other")


@pytest.mark.parametrize("cancelled", [False, True])
async def test_binding_failure_poisoning_is_safe_across_two_native_contexts(tmp_path, cancelled):
    from pyfly.data.relational.sqlalchemy import SessionProvider
    from pyfly.data.transaction import UnexpectedRollbackError
    from tests.audit.test_infrastructure_context import application

    one, two = application(tmp_path / "one.db"), application(tmp_path / "two.db")
    await one.start()
    await two.start()
    try:
        target = AuditRecordingSupport()

        def unavailable():
            if cancelled:
                raise asyncio.CancelledError("fixture-private-value")
            return two.get_bean_by_name("fixture-private-value")

        target._bind_audit_recorder(unavailable)
        with pytest.raises(UnexpectedRollbackError) as rollback:
            async with one.get_bean(SessionProvider).unit():
                with pytest.raises(asyncio.CancelledError if cancelled else AuditError) as refusal:
                    target._audit_recorder("data")
                assert "fixture-private-value" not in str(refusal.value)
                if cancelled:
                    assert refusal.value.args == ()
        assert "fixture-private-value" not in str(rollback.value)
    finally:
        await two.stop()
        await one.stop()


def test_coverage_is_frozen_and_reports_only_configured_actual_di_bindings():
    from pydantic import ValidationError

    from pyfly.container.scoped_proxy import ScopedProxy

    class Adapter(AuditRecordingSupport):
        __pyfly_audit_module__ = "data"

    processor = AuditModulePostProcessor(lambda: object(), modules=frozenset({"data", "web"}))
    assert processor.coverage().di_bound_adapters == ()
    assert processor.coverage().configured_modules == ("data", "web")
    processor.before_init(Adapter(), "fixture")
    snapshot = processor.coverage()
    assert len(snapshot.di_bound_adapters) == 1
    assert snapshot.di_bound_adapters[0].module == "data"
    assert snapshot.di_bound_adapters[0].adapter.endswith(".Adapter")
    with pytest.raises(ValidationError):
        snapshot.configured_modules = ()

    def not_active():
        pytest.fail("A request proxy must not be materialized at singleton startup")

    proxy = ScopedProxy(not_active, Adapter)
    assert processor.before_init(proxy, "scoped") is proxy
    assert processor.coverage() == snapshot
