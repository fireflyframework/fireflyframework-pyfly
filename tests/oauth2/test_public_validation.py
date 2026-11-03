# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Applications share native OAuth validation without private imports."""

import pytest


@pytest.mark.parametrize("uri", ["https://app.example/callback", "https://app.example/callback?flow=connect"])
def test_public_uri_validation_accepts_https(uri):
    from pyfly.oauth2 import validate_oauth_uri

    validate_oauth_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "http://app.example/callback",
        "https://user:secret@app.example",
        "https://app.example/#fragment",
        "https://app.example/\ncallback",
        "https://app.example:bad/callback",
    ],
)
def test_public_uri_validation_rejects_unsafe_addresses(uri):
    from pyfly.oauth2 import validate_oauth_uri

    with pytest.raises(ValueError):
        validate_oauth_uri(uri)


def test_loopback_http_requires_explicit_opt_in():
    from pyfly.oauth2 import validate_oauth_uri

    uri = "http://127.0.0.1:8765/callback"
    with pytest.raises(ValueError):
        validate_oauth_uri(uri)
    validate_oauth_uri(uri, allow_loopback_http=True)


def test_public_scope_serialization_retains_order_and_rejects_injection():
    from pyfly.oauth2 import serialize_oauth_scopes

    assert serialize_oauth_scopes(("openid", "files.read")) == "openid files.read"
    assert serialize_oauth_scopes(()) == ""
    for scopes in (("read write",), ("read\nwrite",), ('read"write',), ("x" * 257,), ("read",) * 65):
        with pytest.raises(ValueError):
            serialize_oauth_scopes(scopes)


async def test_native_client_identity_can_be_bound_without_exposing_credentials():
    from pyfly.oauth2 import OAuth2Client, OAuth2Endpoints

    endpoints = OAuth2Endpoints("https://idp.example/token")
    async with OAuth2Client("application", endpoints, client_secret="synthetic-secret") as client:
        assert client.client_id == "application"
        assert client.endpoints == endpoints
        with pytest.raises(AttributeError):
            client.client_id = "replacement"
