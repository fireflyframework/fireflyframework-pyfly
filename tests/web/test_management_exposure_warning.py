"""Admin keeps legacy defaults while warning about externally bound anonymous access."""

import logging
from contextlib import asynccontextmanager

import pytest

from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.web.adapters.starlette.app import create_app


@asynccontextmanager
async def _lifespan(app):
    yield


@pytest.mark.parametrize(
    "port,lifespan,expected_port,separate",
    [
        (9090, _lifespan, 9090, True),
        (8080, _lifespan, 8080, False),
        (9090, None, 8080, False),
    ],
)
def test_warns_for_actual_anonymous_admin_listener(caplog, port, lifespan, expected_port, separate):
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "admin": {"enabled": True},
                    "server": {"host": "0.0.0.0", "port": 8080},
                    "management": {"server": {"port": port}},
                }
            }
        ),
        auto_configurations=[],
    )
    with caplog.at_level(logging.WARNING):
        create_app(context=context, lifespan=lifespan)
    assert f"0.0.0.0:{expected_port}" in caplog.text
    assert "pyfly.admin.require-auth=true" in caplog.text
    assert ("pyfly.management.security.enabled=true" in caplog.text) is separate
    assert context.config.get("pyfly.admin.require-auth", False) is False

    # Explicit protection, loopback, and disabled management suppress warnings.
    for settings in (
        {"admin": {"enabled": True, "require-auth": True}},
        {"admin": {"enabled": True}, "server": {"host": "127.0.0.1"}},
        {"admin": {"enabled": True}, "server": {"host": "::1"}},
        {"admin": {"enabled": True}, "management": {"server": {"port": -1}}},
        {"admin": {"enabled": False}},
    ):
        caplog.clear()
        safe = ApplicationContext(Config({"pyfly": settings}), auto_configurations=[])
        create_app(context=safe, lifespan=lifespan)
        assert "Admin is externally bound" not in caplog.text
