"""Provider bodies and transport errors must not leak credentials into logs."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import OAuth2LoginHandler


@pytest.mark.parametrize("transport_error", [False, True])
async def test_exchange_errors_are_redacted(caplog, transport_error):
    registration = ClientRegistration("test", "client", token_uri="https://idp.test/token")
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(registration))
    client = AsyncMock()
    if transport_error:
        client.post.side_effect = httpx.ConnectError("secret-sentinel")
    else:
        client.post.return_value = httpx.Response(400, text="secret-sentinel")
    with patch("httpx.AsyncClient") as constructor:
        constructor.return_value.__aenter__.return_value = client
        assert await handler._exchange_code(registration, "code") == {}
    assert "secret-sentinel" not in caplog.text
