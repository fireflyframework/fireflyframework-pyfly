# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Disposable native SQL/DI/JWT audit dashboard, with no external services."""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from pyfly.admin.audit import AdminAuditScopeResolver
from pyfly.admin.auto_configuration import AdminAutoConfiguration
from pyfly.audit import AuditActor, AuditEvent, AuditJournal, AuditResource, AuditScope
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.transaction import infrastructure_unit
from pyfly.security.jwt import JWTService
from pyfly.security.permission import PermissionEvaluator
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filters.security_filter import SecurityFilter

SCOPE = AuditScope(tenant_id="development", workspace_id="research", project_id="approved-project")
TEST_KEY = "test-jwt-signing-key-long-enough-for-hs256"


class Authority:
    async def resolve(self, security, requested_handle):
        if security.user_id != "verified-reader" or requested_handle not in (None, "approved"):
            raise ValueError("Scope unavailable")
        return SCOPE


class Permissions:
    def has_permission(self, context, target, permission, *, target_type=None):
        return target == SCOPE and permission in {"admin:audit:read", "admin:audit:verify"}


context = ApplicationContext(
    Config(
        {
            "pyfly": {
                "admin": {"enabled": True, "require-auth": True, "audit": {"enabled": True}},
                "audit": {"enabled": True},
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": "sqlite+aiosqlite:///" + os.environ["PYFLY_AUDIT_BROWSER_DB"],
                        "ddl-auto": "create",
                    }
                },
            }
        }
    ),
    auto_configurations=[
        DataSourceAutoConfiguration,
        RelationalAutoConfiguration,
        AuditAutoConfiguration,
        AdminAutoConfiguration,
    ],
)
context.container.register_instance(AdminAuditScopeResolver, Authority())
context.container.register_instance(PermissionEvaluator, Permissions())
context.container.register_instance(SecurityFilter, SecurityFilter(JWTService(TEST_KEY)))


@asynccontextmanager
async def lifespan(app):
    await context.start()
    journal = context.get_bean(AuditJournal)
    async with infrastructure_unit(journal.engine):
        for index in range(27):
            await journal.append(
                AuditEvent(
                    event_id=f"fixture-{index}",
                    event_type="agentic.run",
                    scope=SCOPE,
                    actor=AuditActor(kind="service", subject_id="trusted-development-host"),
                    resource=AuditResource(kind="agent", resource_id=f"research-agent-{index}", revision=1),
                    occurred_at=datetime(2026, 10, 31, tzinfo=UTC),
                    outcome="observed",
                )
            )
    try:
        yield
    finally:
        await context.stop()


app = create_app(context=context, docs_enabled=False, actuator_enabled=False, lifespan=lifespan)
