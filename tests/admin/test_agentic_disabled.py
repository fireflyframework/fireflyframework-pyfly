"""Disabled admin introspection must preserve the base-only import boundary."""

import subprocess
import sys


def test_admin_without_agentic_does_not_import_optional_ai_modules():
    code = """
import sys
from pyfly.admin.wiring import build_admin_routes
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from starlette.applications import Starlette
from starlette.testclient import TestClient
ctx = ApplicationContext(Config({}))
routes = build_admin_routes(ctx, admin_trace_collector=None, base_health_agg=None, extra_post_start=[])
with TestClient(Starlette(routes=routes)) as client:
    assert client.get('/admin/api/settings').json()['agenticEnabled'] is False
    assert client.get('/admin/api/agentic').status_code == 404
assert not any(name.startswith(('pydantic_ai', 'pydantic_graph', 'pyfly.agentic')) for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
