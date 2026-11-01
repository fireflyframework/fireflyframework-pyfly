# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Actual native DI and verified JWT routes; no global evaluator or anonymous fallback."""

import importlib
import logging

import pytest
from starlette.testclient import TestClient

from pyfly.admin.audit import AdminAuditProvider, AdminAuditScopeResolver
from pyfly.admin.auto_configuration import AdminAutoConfiguration
from pyfly.audit import AuditJournal
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.security.jwt import JWTService
from pyfly.security.permission import PermissionEvaluator
from pyfly.web.adapters.starlette.filters.security_filter import SecurityFilter
from tests.integration.test_admin_audit import USER, Permissions, Resolver


def application(tmp_path, *, enabled=True, resolver=True, evaluator=True):
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "admin": {"enabled": True, "require-auth": False, "audit": {"enabled": enabled}},
                    "audit": {"enabled": True},
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}",
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
    authority, policy = Resolver(), Permissions()
    if resolver:
        context.container.register_instance(AdminAuditScopeResolver, authority)
    if evaluator:
        context.container.register_instance(PermissionEvaluator, policy)
    jwt = JWTService("test-jwt-signing-key-long-enough-for-hs256")
    context.container.register_instance(SecurityFilter, SecurityFilter(jwt))
    return context, authority, policy, jwt


@pytest.mark.parametrize("dependency", ["resolver", "evaluator"])
async def test_enabled_missing_authority_fails_startup_without_global_handler_leak(tmp_path, dependency):
    original = tuple(logging.getLogger().handlers)
    context, *_ = application(tmp_path, **{dependency: False})
    try:
        with pytest.raises(Exception, match="AdminAuditScopeResolver|PermissionEvaluator"):
            await context.start()
        assert tuple(logging.getLogger().handlers) == original
    finally:
        await context.stop()


async def test_native_admin_handler_is_owned_by_start_stop(tmp_path):
    original = tuple(logging.getLogger().handlers)
    context, *_ = application(tmp_path, enabled=False, resolver=False, evaluator=False)
    await context.start()
    assert len(logging.getLogger().handlers) == len(original) + 1
    await context.stop()
    assert tuple(logging.getLogger().handlers) == original


async def test_two_admin_contexts_stop_only_their_own_handler(tmp_path):
    from pyfly.admin.log_handler import AdminLogHandler

    original = tuple(logging.getLogger().handlers)
    first, *_ = application(tmp_path, enabled=False, resolver=False, evaluator=False)
    second, *_ = application(tmp_path, enabled=False, resolver=False, evaluator=False)
    await first.start()
    await second.start()
    other = second.get_bean(AdminLogHandler)
    await first.stop()
    try:
        assert tuple(logging.getLogger().handlers) == (*original, other)
    finally:
        await second.stop()
    assert tuple(logging.getLogger().handlers) == original


async def test_two_native_context_readers_keep_journals_and_authority_separate(tmp_path):
    from pyfly.data.transaction import infrastructure_unit
    from pyfly.kernel.exceptions import ForbiddenException
    from tests.integration.test_audit_journal import event

    first_path, second_path = tmp_path / "first", tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    first, _, first_policy, _ = application(first_path)
    second, *_ = application(second_path)
    await first.start()
    await second.start()
    try:
        one, two = first.get_bean(AdminAuditProvider), second.get_bean(AdminAuditProvider)
        journal = first.get_bean(AuditJournal)
        assert one is not two and one._journal is not two._journal
        async with infrastructure_unit(journal.engine):
            await journal.append(event("first-only"))
        assert (await one.page(USER)).head.sequence == 1
        assert (await two.page(USER)).head.sequence == 0
        first_policy.allowed = False
        with pytest.raises(ForbiddenException):
            await one.page(USER)
        assert (await two.page(USER)).head.sequence == 0
    finally:
        await first.stop()
        await second.stop()


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
async def test_actual_jwt_routes_reauthorize_and_never_offer_writes(tmp_path, adapter):
    context, authority, policy, jwt = application(tmp_path)
    await context.start()
    try:
        reader = context.get_bean(AdminAuditProvider)
        assert reader._journal is context.get_bean(AuditJournal)
        app = importlib.import_module(f"pyfly.web.adapters.{adapter}.app").create_app(
            context=context, docs_enabled=False
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            path = "/admin/api/audit"
            assert client.get(path).status_code == 401
            client.headers["Authorization"] = "Bearer forged"
            assert client.get(path).status_code == 401
            client.headers["Authorization"] = "Bearer " + jwt.encode({"sub": USER.user_id, "roles": ["ADMIN"]})
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["verification"] == "not_verified"
            assert client.get(path + "?scope=other").status_code == 403
            assert client.get(path + "?limit=101").status_code == 422
            assert client.get(path + "?scope=mine&scope=other").status_code == 422
            assert client.get(path + "?query=fixture-private-token").status_code == 422
            assert client.post(path).status_code == 405
            policy.allowed = False
            assert client.get(path).status_code == 403
            policy.allowed = True
            authority.revoked = True
            response = client.get(path)
            assert response.status_code == 403
            assert "fixture private" not in response.text
    finally:
        await context.stop()
