# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Opt-in recording uses the host's verified resolver and native context-owned beans."""

import importlib

import pytest

from pyfly.audit import (
    AuditActor,
    AuditContext,
    AuditContextResolver,
    AuditJournal,
    AuditRecorder,
    AuditResource,
    AuditScope,
)
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy import SqlAlchemyTransactionManager


@configuration
class HostResolver:
    @bean
    def verified_audit_context(self) -> AuditContextResolver:
        class Resolver:
            async def resolve(self, operation):
                return AuditContext(
                    scope=AuditScope(tenant_id="tenant", workspace_id="workspace", project_id="project"),
                    actor=AuditActor(kind="service", subject_id="verified-host"),
                    resource=AuditResource(kind="fixture", resource_id="trusted"),
                )

        return Resolver()


@configuration
class OtherHostResolver:
    @bean
    def conflicting_audit_context(self) -> AuditContextResolver:
        return HostResolver().verified_audit_context()


def application(path, *, enabled=True, modules=None, resolver=True):
    module = importlib.import_module("pyfly.audit.recording_auto_configuration")
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "audit": {
                        "enabled": True,
                        "recording": {"enabled": enabled, "modules": modules if modules is not None else ["data"]},
                    },
                    "data": {
                        "relational": {"enabled": True, "url": f"sqlite+aiosqlite:///{path}", "ddl-auto": "create"}
                    },
                }
            }
        ),
        auto_configurations=[
            DataSourceAutoConfiguration,
            RelationalAutoConfiguration,
            AuditAutoConfiguration,
            module.AuditRecordingAutoConfiguration,
        ],
    )
    if resolver:
        context.register_bean(HostResolver)
    return context


async def test_disabled_recording_has_no_resolver_dependency(tmp_path):
    from pyfly.container.exceptions import NoSuchBeanError

    context = application(tmp_path / "disabled.db", enabled=False, resolver=False)
    await context.start()
    try:
        with pytest.raises(NoSuchBeanError):
            context.get_bean(AuditRecorder)
    finally:
        await context.stop()


async def test_enabled_recording_requires_host_resolver_and_rejects_unknown_auto_modules(tmp_path):
    context = application(tmp_path / "missing.db", resolver=False)
    with pytest.raises(Exception, match="AuditContextResolver"):
        await context.start()
    context = application(tmp_path / "typo.db", modules=["agentics"])
    with pytest.raises(Exception, match="supported automatic audit modules"):
        await context.start()


async def test_ambiguous_host_resolver_is_not_replaced_by_a_default(tmp_path):
    from pyfly.container.exceptions import NoUniqueBeanError

    context = application(tmp_path / "ambiguous.db")
    context.register_bean(OtherHostResolver)
    with pytest.raises(NoUniqueBeanError, match="AuditContextResolver"):
        await context.start()


async def test_two_contexts_recorders_resolve_their_exact_native_managers(tmp_path):
    first, second = application(tmp_path / "one.db"), application(tmp_path / "two.db")
    await first.start()
    await second.start()
    try:
        for context in (first, second):
            recorder = context.get_bean(AuditRecorder)
            assert recorder.manager is context.get_bean(SqlAlchemyTransactionManager)
            assert recorder.journal is context.get_bean(AuditJournal)
            assert recorder.modules == frozenset({"data"})
        assert first.get_bean(AuditRecorder) is not second.get_bean(AuditRecorder)
    finally:
        await second.stop()
        await first.stop()
