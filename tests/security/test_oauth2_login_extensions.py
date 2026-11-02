# Copyright 2026 Firefly Software Foundation & Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.security.oauth2.resource_server import ClaimMappings, JWKSTokenValidator, build_security_context
from pyfly.session.session import HttpSession
from tests.security._session_callbacks import stored_callback
from tests.security.test_oauth2_resource_server import _mint
from tests.security.test_oauth2_resource_server import jwks as jwks


def request(query=""):
    result = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/callback",
            "headers": [],
            "query_string": query.encode(),
            "path_params": {"registration_id": "acme"},
        }
    )
    result.state.session = HttpSession("before", {})
    result.state.session.set_attribute("oauth2_state", "state")
    result.state.session.set_attribute("oauth2_nonce", "nonce")
    return result


def registration(uri="https://idp.test/jwks", issuer="https://idp.test"):
    return ClientRegistration(
        "acme",
        "client",
        client_secret="never-log-this",
        authorization_uri=issuer + "/authorize",
        redirect_uri="https://app.test/callback",
        jwks_uri=uri,
        issuer_uri=issuer,
        scopes=["openid"],
    )


def test_registration_repr_redacts_credentials_and_raw_claims_are_opt_in_copied():
    assert "never-log-this" not in repr(registration())
    claims = {"sub": "user", "auth_time": 123, "groups": ["a", "b"]}
    mappings = ClaimMappings(raw_attribute_claims=("auth_time", "groups"))
    context = build_security_context(claims, mappings)
    claims["groups"].append("changed")
    assert context.attributes == {"auth_time": 123, "groups": ["a", "b"]}


async def test_customizer_can_add_hints_but_never_override_native_state():
    reg = registration()
    customizer = SimpleNamespace(customize=AsyncMock(return_value={"kc_idp_hint": "approved", "prompt": "login"}))
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(reg), authorization_request_customizer=customizer)
    incoming = request()
    response = await handler._handle_authorization(incoming)
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["kc_idp_hint"] == ["approved"] and params["code_challenge_method"] == ["S256"]
    assert params["state"] == [incoming.state.session.get_attribute("oauth2_state")]
    customizer.customize.return_value = {"state": "forbidden"}
    with pytest.raises(SecurityException, match="Invalid authorization customization"):
        await handler._handle_authorization(request())


async def test_success_hook_receives_verified_claims_before_session_publication(jwks, monkeypatch):
    uri, issuer, _ = jwks
    reg = registration(uri, issuer)
    validator = JWKSTokenValidator(uri, issuer=issuer, audiences=["client"])
    validate = Mock(wraps=validator.validate)
    monkeypatch.setattr(validator, "validate", validate)
    factory = SimpleNamespace(create=Mock(return_value=validator))
    seen = []

    async def success(req, registration, context):
        assert (
            req.state.session.claim_bound
            and req.state.session.id != "before"
            and req.state.session.get_attribute("SECURITY_CONTEXT") is None
        )
        assert context.user_id == "user" and context.attributes["auth_time"] == 123
        seen.append("hook")

    handler = OAuth2LoginHandler(
        InMemoryClientRegistrationRepository(reg),
        authentication_success_handler=SimpleNamespace(on_success=success),
        id_token_validator_factory=factory,
        claim_mappings=ClaimMappings(principal_claims=("sub",), raw_attribute_claims=("auth_time",)),
        require_oidc_id_token=True,
    )
    token = _mint({"iss": issuer, "aud": "client", "sub": "user", "nonce": "nonce", "auth_time": 123})
    monkeypatch.setattr(
        handler, "_exchange_code", AsyncMock(return_value={"access_token": "owned-test", "id_token": token})
    )
    incoming = request("state=state&code=code")
    response = await stored_callback(handler, incoming)
    assert response.status_code == 302 and seen == ["hook"]
    assert (
        incoming.state.session.id != "before"
        and incoming.state.session.get_attribute("SECURITY_CONTEXT").user_id == "user"
    )
    assert validate.call_count == 1


async def test_strict_oidc_never_uses_userinfo_as_identity_fallback(monkeypatch):
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(registration()), require_oidc_id_token=True)
    monkeypatch.setattr(handler, "_exchange_code", AsyncMock(return_value={"access_token": "owned-test"}))
    fallback = AsyncMock(return_value={"sub": "unverified"})
    monkeypatch.setattr(handler, "_fetch_user_info", fallback)
    incoming = request("state=state&code=code")
    response = await stored_callback(handler, incoming)
    assert response.status_code == 401 and incoming.state.session.claim_bound
    assert incoming.state.session.get_attribute("SECURITY_CONTEXT") is None
    fallback.assert_not_awaited()


async def test_native_context_discovers_optional_ports_and_login_claim_configuration():
    from pyfly.context import ApplicationContext
    from pyfly.core import Config
    from pyfly.security.oauth2 import OAuth2AuthorizationRequestCustomizer

    class Customizer(OAuth2AuthorizationRequestCustomizer):
        async def customize(self, request, registration):
            return {"prompt": "login"}

    customizer = Customizer()
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "banner": {"mode": "off"},
                    "security": {
                        "oauth2": {
                            "login": {
                                "enabled": True,
                                "require-oidc-id-token": True,
                                "principal-claim-names": ["sub"],
                                "raw-attribute-claims": ["auth_time", "groups"],
                            },
                            "client": {
                                "enabled": True,
                                "registrations": {
                                    "acme": {"client-id": "client", "authorization-uri": "https://idp.test/auth"}
                                },
                            },
                        }
                    },
                }
            }
        )
    )
    context.container.register_instance(OAuth2AuthorizationRequestCustomizer, customizer)
    await context.start()
    try:
        handler = context.get_bean(OAuth2LoginHandler)
        assert handler._authorization_request_customizer is customizer
        assert handler._require_oidc_id_token is True
        assert handler._claim_mappings.raw_attribute_claims == ("auth_time", "groups")
        assert handler._claim_mappings.principal_claims == ("sub",)
    finally:
        await context.stop()


async def test_success_denial_consumes_challenge_without_publishing_or_registering_identity(jwks, monkeypatch):
    from pyfly.kernel.exceptions import SecurityException

    uri, issuer, _ = jwks
    hook = AsyncMock(side_effect=SecurityException("Policy denied", code="ACCESS_DENIED"))
    concurrency = SimpleNamespace(on_login=AsyncMock())
    handler = OAuth2LoginHandler(
        InMemoryClientRegistrationRepository(registration(uri, issuer)),
        concurrency=concurrency,
        authentication_success_handler=SimpleNamespace(on_success=hook),
        require_oidc_id_token=True,
    )
    token = _mint({"iss": issuer, "aud": "client", "sub": "user", "nonce": "nonce"})
    monkeypatch.setattr(
        handler, "_exchange_code", AsyncMock(return_value={"access_token": "owned-test", "id_token": token})
    )
    incoming = request("state=state&code=code")
    with pytest.raises(SecurityException):
        await stored_callback(handler, incoming)
    assert incoming.state.session.claim_bound and incoming.state.session.get_attribute("SECURITY_CONTEXT") is None
    concurrency.on_login.assert_not_awaited()


@pytest.mark.parametrize(
    "changes,nonce",
    [
        ({"nonce": "different"}, "nonce"),
        ({}, None),
        ({"aud": ["client", "second"]}, "nonce"),
        ({"azp": "different"}, "nonce"),
    ],
)
async def test_native_nonce_and_authorized_party_checks_precede_success(jwks, monkeypatch, changes, nonce):
    uri, issuer, _ = jwks
    hook = AsyncMock()
    handler = OAuth2LoginHandler(
        InMemoryClientRegistrationRepository(registration(uri, issuer)),
        require_oidc_id_token=True,
        authentication_success_handler=SimpleNamespace(on_success=hook),
    )
    claims = {"iss": issuer, "aud": "client", "sub": "user", "nonce": "nonce", **changes}
    monkeypatch.setattr(
        handler, "_exchange_code", AsyncMock(return_value={"access_token": "owned-test", "id_token": _mint(claims)})
    )
    incoming = request("state=state&code=code")
    if nonce is None:
        incoming.state.session.remove_attribute("oauth2_nonce")
    assert (await stored_callback(handler, incoming)).status_code == 401
    hook.assert_not_awaited()
    assert incoming.state.session.get_attribute("SECURITY_CONTEXT") is None
