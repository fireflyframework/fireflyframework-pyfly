# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Admin read ports do not import optional SQL, AI or crypto adapters."""

import os
import subprocess
import sys

import pytest

SCRIPT = r"""
import asyncio
import importlib.abc
import sys

class BlockVendors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'aiosqlite', 'pydantic_ai', 'cryptography'}:
            raise ModuleNotFoundError('Optional vendor absent in Admin contract', name=fullname)

sys.meta_path.insert(0, BlockVendors())
from pyfly.context import ApplicationContext
from pyfly.core import Config

async def main():
    mode = sys.argv[1]
    context = ApplicationContext(Config({'pyfly': {'admin': {
        'enabled': mode != 'disabled', 'audit': {'enabled': mode == 'custom-audit'},
    }}}))
    if mode == 'custom-audit':
        from pyfly.admin.audit import AdminAuditProvider, AdminAuditScopeResolver
        from pyfly.audit import AuditJournal, AuditScope
        from pyfly.security.permission import PermissionEvaluator

        class Journal:
            async def start(self): pass
            async def stop(self): pass
            async def append(self, event): raise AssertionError('No writes')
            async def head(self, scope): raise AssertionError('No reads at startup')
            async def read(self, scope, **kwargs): raise AssertionError('No reads at startup')
            async def verify(self, scope, **kwargs): raise AssertionError('No reads at startup')
            async def export(self, scope, **kwargs): raise AssertionError('No exports')

        class Resolver:
            async def resolve(self, security, requested_handle):
                return AuditScope(tenant_id='test', workspace_id='test', project_id='test')

        class Policy:
            def has_permission(self, *args, **kwargs): return False

        journal = Journal()
        context.container.register_instance(AuditJournal, journal)
        context.container.register_instance(AdminAuditScopeResolver, Resolver())
        context.container.register_instance(PermissionEvaluator, Policy())
    await context.start()
    try:
        if mode != 'disabled':
            from pyfly.admin.log_handler import AdminLogHandler
            from pyfly.admin.wiring import build_admin_routes
            assert context.get_bean(AdminLogHandler) is not None
            assert build_admin_routes(context, admin_trace_collector=None, base_health_agg=None, extra_post_start=[])
        if mode == 'custom-audit':
            assert context.get_bean(AdminAuditProvider)._journal is journal
        assert not {'sqlalchemy', 'aiosqlite', 'pydantic_ai', 'cryptography'}.intersection(sys.modules)
    finally:
        await context.stop()

asyncio.run(main())
"""


@pytest.mark.parametrize("mode", ["disabled", "admin", "custom-audit"])
def test_optional_vendor_absence_does_not_disable_native_admin(mode):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", SCRIPT, mode], env=environment, capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
