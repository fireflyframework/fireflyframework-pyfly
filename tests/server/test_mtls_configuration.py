# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native server adapters enforce configured client authentication."""

from __future__ import annotations

import ssl
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pyfly.config.properties.server import ServerProperties
from pyfly.server.launcher import validate_server_properties


def properties(tmp_path):
    for name in ("server.pem", "server.key", "ca.pem"):
        (tmp_path / name).write_text("constructor-only inert fixture")
    return ServerProperties(
        ssl_certfile=str(tmp_path / "server.pem"),
        ssl_keyfile=str(tmp_path / "server.key"),
        ssl_ca_certs=str(tmp_path / "ca.pem"),
        ssl_cert_reqs="required",
    )


def test_required_client_identity_validates_ca_and_server_identity():
    with pytest.raises(ValueError, match="client authentication"):
        validate_server_properties(ServerProperties(ssl_cert_reqs="required"))
    with pytest.raises(ValueError, match="ssl-cert-reqs"):
        validate_server_properties(ServerProperties(ssl_cert_reqs="optional"))


def test_uvicorn_maps_required_client_auth(tmp_path):
    from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

    props = properties(tmp_path)
    validate_server_properties(props)
    kwargs = UvicornServerAdapter._build_kwargs("127.0.0.1", 0, "asyncio", props)
    assert kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert kwargs["ssl_ca_certs"] == props.ssl_ca_certs


@pytest.mark.asyncio
async def test_hypercorn_maps_required_client_auth(tmp_path, monkeypatch):
    import hypercorn.asyncio

    from pyfly.server.adapters.hypercorn.adapter import HypercornServerAdapter

    runner = AsyncMock()
    monkeypatch.setattr(hypercorn.asyncio, "serve", runner)
    props = properties(tmp_path)
    await HypercornServerAdapter().serve_async("inert:app", props)
    config = runner.call_args.args[1]
    assert config.verify_mode == ssl.CERT_REQUIRED
    assert config.ca_certs == props.ssl_ca_certs


def test_granian_rejects_unsupported_client_auth_before_start(tmp_path, monkeypatch):
    from pyfly.server.adapters.granian.adapter import GranianServerAdapter

    granian, constants = ModuleType("granian"), ModuleType("granian.constants")
    granian.Granian = lambda **kwargs: pytest.fail("must reject before constructing a server")
    constants.Interfaces = SimpleNamespace(ASGI="asgi")
    monkeypatch.setitem(sys.modules, "granian", granian)
    monkeypatch.setitem(sys.modules, "granian.constants", constants)
    with pytest.raises(ValueError, match="client authentication"):
        GranianServerAdapter().serve("inert:app", properties(tmp_path))


def test_launcher_rejects_unadvertised_custom_server_before_loop_install(tmp_path):
    from pyfly.server.launcher import launch

    class UnknownServer:
        def serve(self, app, config):
            pytest.fail("unsupported server must not start")

    class Loop:
        def install(self):
            pytest.fail("validation must precede policy mutation")

    with pytest.raises(ValueError, match="client authentication"):
        launch("inert:app", properties(tmp_path), server=UnknownServer(), event_loop=Loop())
