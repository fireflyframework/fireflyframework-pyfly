# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""TLS identity and transport policy apply to every native HTTP factory."""

from __future__ import annotations

import ssl

import pytest

from pyfly.client.auto_configuration import ClientAutoConfiguration
from pyfly.config.properties.client import ClientTlsProperties
from pyfly.core.config import Config


def test_native_tls_context_requires_server_verification():
    context = ClientTlsProperties().ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("values", [{"certfile": "client.pem"}, {"keyfile": "key.pem"}])
def test_unpaired_tls_identity_fails_before_constructing_client(values):
    with pytest.raises(ValueError, match="certificate.*key"):
        ClientTlsProperties(**values).ssl_context()


def test_shared_and_declarative_factories_preserve_all_transport_policy(monkeypatch):
    import pyfly.client.adapters.httpx_adapter as module

    calls = []
    monkeypatch.setattr(module, "HttpxClientAdapter", lambda **kwargs: calls.append(kwargs) or kwargs)
    config = Config(
        {
            "pyfly": {
                "client": {
                    "timeout": 12.5,
                    "trust-env": False,
                    "follow-redirects": True,
                    "headers": {"x-native": "configured"},
                    "transport-retries": 2,
                    "tls": {"ca-file": "ca.pem", "certfile": "client.pem", "keyfile": "key.pem"},
                }
            }
        }
    )
    auto = ClientAutoConfiguration()
    auto.http_client_adapter(config)
    auto.http_client_post_processor(config)._factory("https://service.example")
    shared, declarative = calls
    assert declarative == {**shared, "base_url": "https://service.example"}
    assert shared["trust_env"] is False
    assert shared["headers"] == {"x-native": "configured"}
    assert shared["retries"] == 2
    assert shared["follow_redirects"] is True
    assert shared["timeout"].total_seconds() == 12.5
    assert shared["tls"] == ClientTlsProperties(ca_file="ca.pem", certfile="client.pem", keyfile="key.pem")


def test_explicit_context_and_typed_tls_are_mutually_exclusive():
    from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

    with pytest.raises(ValueError, match="TLS"):
        HttpxClientAdapter(tls=ClientTlsProperties(), verify=ssl.create_default_context())


@pytest.mark.asyncio
async def test_typed_tls_rejects_plaintext_and_redirect_downgrade_before_transport():
    import httpx

    from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

    seen = []

    async def handle(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://downgrade.invalid/private"})

    client = HttpxClientAdapter(tls=ClientTlsProperties(), trust_env=False, follow_redirects=True)
    original = client._client._transport
    client._client._transport = httpx.MockTransport(handle)
    await original.aclose()
    try:
        with pytest.raises(ValueError, match="HTTPS"):
            await client.request("GET", "http://downgrade.invalid/private")
        assert seen == []
        with pytest.raises(ValueError, match="HTTPS"):
            await client.request("GET", "https://verified.invalid/private")
        assert seen == ["https://verified.invalid/private"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_internal_https_guard_preserves_bounded_cookie_suppressed_requests():
    import httpx

    from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

    async def handle(request):
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"ok")

    client = HttpxClientAdapter(tls=ClientTlsProperties(), trust_env=False)
    original = client._client._transport
    client._client._transport = httpx.MockTransport(handle)
    await original.aclose()
    client._client.cookies.set("session", "private")
    try:
        result = await client.request_bounded(
            "GET", "https://verified.invalid", max_response_bytes=2, suppress_cookies=True
        )
        assert result.content == b"ok"
        async with client.stream("GET", "https://verified.invalid", suppress_cookies=True) as response:
            assert (await response.aread()) == b"ok"
    finally:
        await client.stop()


def test_explicit_verified_context_propagates_through_both_factories(monkeypatch):
    import pyfly.client.adapters.httpx_adapter as module

    calls = []
    monkeypatch.setattr(module, "HttpxClientAdapter", lambda **kwargs: calls.append(kwargs) or kwargs)
    context = ssl.create_default_context()
    auto = ClientAutoConfiguration()
    config = Config({})
    auto.http_client_adapter(config, ssl_context=context)
    auto.http_client_post_processor(config, ssl_context=context)._factory("https://verified.invalid")
    assert calls[0]["verify"] is context
    assert calls[1]["verify"] is context
    assert calls[0]["tls"] is None
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verification"):
        auto.http_client_adapter(config, ssl_context=context)
    with pytest.raises(ValueError, match="TLS"):
        auto.http_client_adapter(
            Config({"pyfly": {"client": {"tls": {"ca-file": "ca.pem"}}}}), ssl_context=ssl.create_default_context()
        )


@pytest.mark.asyncio
async def test_named_ssl_context_is_selected_by_native_di_for_both_factories():
    from pyfly.client import HttpClientPort
    from pyfly.client.post_processor import HttpClientBeanPostProcessor
    from pyfly.container import bean, configuration
    from pyfly.context.application_context import ApplicationContext

    tls = ssl.create_default_context()

    @configuration
    class TlsConfiguration:
        @bean(name="http_client_ssl_context")
        def context(self) -> ssl.SSLContext:
            return tls

    context = ApplicationContext(Config({}), auto_configurations=[ClientAutoConfiguration])
    context.register_bean(TlsConfiguration)
    await context.start()
    declarative = None
    try:
        shared = context.get_bean(HttpClientPort)
        declarative = context.get_bean(HttpClientBeanPostProcessor)._factory("https://verified.invalid")
        assert shared._client._transport._pool._ssl_context is tls
        assert declarative._client._transport._pool._ssl_context is tls
    finally:
        if declarative is not None:
            await declarative.stop()
        await context.stop()


@pytest.mark.parametrize("value", ["", False, 0, "bad\x00path"])
def test_explicit_tls_paths_cannot_silently_disable_verification(value):
    with pytest.raises(ValueError, match="TLS.*path"):
        ClientTlsProperties(ca_file=value)


def test_existing_positional_property_constructors_keep_their_meaning():
    from pyfly.config.properties.client import ClientProperties
    from pyfly.config.properties.server import ServerProperties

    client = ClientProperties(7, False, True, 2, {"x-native": "configured"})
    assert (client.timeout, client.trust_env, client.follow_redirects, client.transport_retries) == (7, False, True, 2)
    assert client.headers == {"x-native": "configured"}
    assert client.tls == ClientTlsProperties()
    server = ServerProperties("127.0.0.1", 8443, "uvicorn", "asyncio", 1, 128, 30, "auto", None, None, 17, 32, 99)
    assert (server.keep_alive_timeout, server.max_concurrent_connections, server.max_requests_per_worker) == (
        17,
        32,
        99,
    )
    assert server.ssl_cert_reqs == "none" and server.ssl_ca_certs is None
