# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Configuration diagnostics never expose authentication header values."""

import json

import pytest

from pyfly.core.config import Config


@pytest.mark.parametrize(
    "header", ["Authorization", "authorization", "Proxy-Authorization", "proxy_authorization", "Cookie", "Set-Cookie"]
)
def test_known_sensitive_header_leaves_are_masked(header):
    assert Config({}).mask_value("pyfly.client.headers." + header, "PRIVATE_HEADER_SENTINEL") == "******"
    assert Config({}).mask_value("pyfly.client.headers.X-Request-Id", "diagnostic") == "diagnostic"


@pytest.mark.parametrize("header", ["AUTHORIZATION", "PROXY_AUTHORIZATION", "COOKIE", "SET_COOKIE"])
def test_header_environment_sources_are_masked(header, monkeypatch):
    monkeypatch.setenv("PYFLY_CLIENT_HEADERS_" + header, "PRIVATE_HEADER_SENTINEL")
    assert "PRIVATE_HEADER_SENTINEL" not in json.dumps(Config({}).property_sources())


@pytest.mark.asyncio
async def test_bound_client_properties_and_file_property_sources_mask_headers():
    from pyfly.actuator.endpoints.configprops_endpoint import ConfigPropsEndpoint
    from pyfly.context.application_context import ApplicationContext

    config = Config(
        {
            "pyfly": {
                "client": {
                    "headers": {
                        "Authorization": "PRIVATE_HEADER_SENTINEL",
                        "Proxy-Authorization": "PRIVATE_HEADER_SENTINEL",
                        "Cookie": "PRIVATE_HEADER_SENTINEL",
                        "Set-Cookie": "PRIVATE_HEADER_SENTINEL",
                        "X-Request-Id": "diagnostic",
                    }
                }
            }
        }
    )
    context = ApplicationContext(config, auto_configurations=[])
    diagnostic = await ConfigPropsEndpoint(context).handle()
    assert "PRIVATE_HEADER_SENTINEL" not in json.dumps(diagnostic)
    assert "diagnostic" in json.dumps(diagnostic)
    assert "PRIVATE_HEADER_SENTINEL" not in json.dumps(config.property_sources())
    assert config.get("pyfly.client.headers.Authorization") == "PRIVATE_HEADER_SENTINEL"
