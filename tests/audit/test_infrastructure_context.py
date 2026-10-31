# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""An infrastructure operation cannot suppress an unrelated application journal."""

import asyncio
import inspect
from datetime import UTC, datetime

import pytest

from pyfly.audit import AuditActor, AuditError, AuditEvent, AuditJournal, AuditResource, AuditScope
from pyfly.audit._infrastructure import audit_infrastructure, is_audit_infrastructure
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.container.scoped_proxy import ScopedProxy
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy import SessionProvider
from pyfly.data.transaction import UnexpectedRollbackError


def application(path):
    return ApplicationContext(
        Config(
            {
                "pyfly": {
                    "audit": {"enabled": True},
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": f"sqlite+aiosqlite:///{path}",
                            "ddl-auto": "create",
                        }
                    },
                }
            }
        ),
        auto_configurations=[DataSourceAutoConfiguration, RelationalAutoConfiguration, AuditAutoConfiguration],
    )


async def test_two_native_contexts_and_proxies_share_only_their_own_infrastructure_scope(tmp_path):
    assert "owner" in inspect.signature(audit_infrastructure).parameters
    one, two = application(tmp_path / "one.db"), application(tmp_path / "two.db")
    await one.start()
    await two.start()
    try:
        a, b = one.get_bean(AuditJournal), two.get_bean(AuditJournal)
        proxy_a, proxy_b = ScopedProxy(lambda: a, type(a)), ScopedProxy(lambda: b, type(b))
        scope = AuditScope(tenant_id="fixture", workspace_id="workspace", project_id="project")
        with audit_infrastructure(proxy_a):
            assert is_audit_infrastructure(a)
            assert is_audit_infrastructure(proxy_a)
            assert not is_audit_infrastructure(b)
            assert not is_audit_infrastructure(proxy_b)
            async with two.get_bean(SessionProvider).unit():
                await b.append(
                    AuditEvent(
                        event_id="other-application",
                        event_type="fixture.changed",
                        scope=scope,
                        actor=AuditActor(kind="service", subject_id="verified-fixture"),
                        resource=AuditResource(kind="fixture", resource_id="record"),
                        occurred_at=datetime.now(UTC),
                        outcome="admitted",
                    )
                )

            async def child():
                assert is_audit_infrastructure(proxy_a)
                assert not is_audit_infrastructure(proxy_b)

            await asyncio.create_task(child())
        assert len(await b.read(scope)) == 1
        assert not await a.read(scope)
        assert not is_audit_infrastructure(a)
        assert not is_audit_infrastructure(b)
    finally:
        await two.stop()
        await one.stop()


@pytest.mark.parametrize("operation", ["append", "head", "read", "verify", "export"])
async def test_journal_refuses_same_named_unit_from_another_native_context(tmp_path, operation):
    one, two = application(tmp_path / "one.db"), application(tmp_path / "two.db")
    await one.start()
    await two.start()
    try:
        a, b = one.get_bean(AuditJournal), two.get_bean(AuditJournal)
        scope = AuditScope(tenant_id="fixture", workspace_id="workspace", project_id="project")
        fact = AuditEvent(
            event_id="app-a-fact",
            event_type="fixture.changed",
            scope=scope,
            actor=AuditActor(kind="service", subject_id="verified-fixture"),
            resource=AuditResource(kind="fixture", resource_id="record"),
            occurred_at=datetime.now(UTC),
            outcome="admitted",
        )
        async with one.get_bean(SessionProvider).unit():
            await a.append(fact)

        async def wrong_context():
            async with one.get_bean(SessionProvider).unit():
                if operation == "append":
                    await a.append(fact.model_copy(update={"event_id": "must-rollback"}))
                with pytest.raises(AuditError, match="different native Data manager"):
                    if operation == "append":
                        await b.append(fact)
                    elif operation == "export":
                        await b.export(scope, through_sequence=1)
                    else:
                        await getattr(b, operation)(scope)

        if operation == "append":
            with pytest.raises(UnexpectedRollbackError):
                await wrong_context()
        else:
            await wrong_context()
        assert len(await a.read(scope)) == 1
        assert not await b.read(scope)
    finally:
        await two.stop()
        await one.stop()
