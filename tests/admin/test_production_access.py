"""Production Admin credentials resolve secrets before authenticating operators."""

import httpx
import pytest

from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.security.auto_configuration import HttpBasicAutoConfiguration
from pyfly.security.password import BcryptPasswordEncoder
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.management_app import create_management_app


@pytest.mark.parametrize("separate", [False, True], ids=["shared", "separate"])
async def test_admin_authenticates_password_hash_from_environment(monkeypatch, separate):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", BcryptPasswordEncoder(rounds=4).hash("operator-test-password"))
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "admin": {"enabled": True, "require-auth": True, "allowed-roles": ["OPS"]},
                    "management": {"security": {"enabled": True}},
                    "security": {
                        "http-basic": {
                            "enabled": True,
                            "error-mode": "401",
                            "users": {"operator": {"password-hash": "${ADMIN_PASSWORD_HASH}", "roles": "OPS"}},
                        },
                    },
                }
            }
        ),
        auto_configurations=[HttpBasicAutoConfiguration],
    )
    await context.start()
    try:
        if separate:
            app = create_management_app(
                context,
                health_agg=None,
                http_exchange_recorder=None,
                admin_trace_collector=None,
                actuator_active=False,
                admin_enabled=True,
            )
        else:
            app = create_app(context=context, docs_enabled=False)
            app.state.pyfly_install_dynamic_wiring()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            response = await client.get("/admin/api/overview", auth=("operator", "operator-test-password"))
        assert response.status_code == 200, response.text
        assert "app" in response.json()
    finally:
        await context.stop()
