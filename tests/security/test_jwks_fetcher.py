"""Borrowed JWKS fetching retains framework bounds and cooperative caller budgets."""

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import jwt
import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2 import JWKSTokenValidator
from tests.security.test_oauth2_resource_server import KEY1, KEY2, _jwk, _mint
from tests.security.test_oauth2_resource_server import jwks as jwks_endpoint  # noqa: F401

URI = "https://fixed.example/keys"
BODY = json.dumps({"keys": [_jwk(KEY1.public_key(), "k1")]}).encode()


def test_borrowed_fetcher_uses_only_fixed_uri_and_framework_limits():
    calls = []

    class Fetcher:
        def __call__(self, uri, *, timeout, max_bytes):
            calls.append((uri, timeout, max_bytes))
            return BODY

        def close(self):
            raise AssertionError("borrowed fetcher closed")

    validator = JWKSTokenValidator(URI, jwks_fetcher=Fetcher(), jwks_timeout=5, jwks_max_bytes=1000)
    token = jwt.encode(
        {"exp": 9999999999, "sub": "u"},
        KEY1,
        algorithm="RS256",
        headers={"kid": "k1", "jku": "https://untrusted.example/keys", "x5u": "file:///secret"},
    )
    assert validator.validate(token, timeout=2)["sub"] == "u"
    assert validator.validate(token, timeout=2)["sub"] == "u"
    assert len(calls) == 1
    assert calls[0][0] == URI
    assert 0 < calls[0][1] <= 2
    assert calls[0][2] == 1000


def test_fetcher_protocol_is_public_and_accepts_structural_callable():
    from pyfly.security.oauth2 import JWKSFetcher

    def fetch(uri: str, *, timeout: float, max_bytes: int) -> bytes:
        return BODY

    fetcher: JWKSFetcher = fetch
    assert JWKSTokenValidator(URI, jwks_fetcher=fetcher).validate(_mint({}))["exp"]


@pytest.mark.parametrize(
    "document", [b"not json", b"[]", b'{"keys":null}', b'{"keys":[null]}', b'{"keys":[]}', {}, "{}"]
)
def test_custom_fetcher_does_not_bypass_document_validation(document):
    validator = JWKSTokenValidator(URI, jwks_fetcher=lambda uri, **kwargs: document)
    with pytest.raises(SecurityException) as caught:
        validator.validate(_mint({}), timeout=1)
    assert caught.value.code == "INVALID_TOKEN"


def test_custom_fetcher_byte_and_key_count_limits_are_enforced():
    for options in ({"jwks_max_bytes": len(BODY) - 1}, {"jwks_max_keys": 1}):
        document = (
            BODY
            if "jwks_max_bytes" in options
            else json.dumps(
                {
                    "keys": [
                        _jwk(KEY1.public_key(), "k1"),
                        _jwk(KEY2.public_key(), "k2"),
                    ]
                }
            ).encode()
        )
        validator = JWKSTokenValidator(URI, jwks_fetcher=lambda uri, document=document, **kwargs: document, **options)
        with pytest.raises(SecurityException):
            validator.validate(_mint({}), timeout=1)


@pytest.mark.parametrize(
    "change", [{"kid": "other"}, {"alg": "RS384"}, {"n": ""}, {"use": "enc"}, {"key_ops": ["sign"]}]
)
def test_custom_fetcher_still_requires_matching_valid_signing_key(change):
    body = json.dumps({"keys": [{**_jwk(KEY1.public_key(), "k1"), **change}]}).encode()
    validator = JWKSTokenValidator(URI, jwks_fetcher=lambda uri, **kwargs: body)
    with pytest.raises(SecurityException):
        validator.validate(_mint({}), timeout=1)


def test_custom_fetch_failure_is_redacted_in_exception_and_traceback():
    secret = "FETCHER-CREDENTIAL-SENTINEL"

    def fetch(uri, **kwargs):
        raise RuntimeError(secret)

    with pytest.raises(SecurityException) as caught:
        JWKSTokenValidator(URI, jwks_fetcher=fetch).validate(_mint({}), timeout=1)
    assert caught.value.code == "INVALID_TOKEN"
    assert secret not in str(caught.value) + repr(caught.value) + "".join(traceback.format_exception(caught.value))
    cause = caught.value.__cause__
    while cause is not None:
        assert secret not in str(cause)
        cause = cause.__cause__


@pytest.mark.parametrize("budget", [0, -1, True, float("nan"), float("inf"), "1"])
def test_invalid_caller_budget_fails_before_fetch(budget):
    calls = []
    validator = JWKSTokenValidator(URI, jwks_fetcher=lambda uri, **kwargs: calls.append(uri))
    with pytest.raises(ValueError):
        validator.validate(_mint({}), timeout=budget)
    assert calls == []


def test_waiter_deadline_does_not_start_fetch_or_change_refresh_state():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fetch(uri, **kwargs):
        calls.append(uri)
        entered.set()
        assert release.wait(2)
        return BODY

    validator = JWKSTokenValidator(URI, jwks_fetcher=fetch)
    token = _mint({"sub": "u"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(validator.validate, token, timeout=1)
        assert entered.wait(1)
        try:
            waiter = pool.submit(validator.validate, token, timeout=0.02)
            with pytest.raises(SecurityException):
                waiter.result(timeout=0.5)
            assert calls == [URI]
        finally:
            release.set()
        assert first.result(timeout=1)["sub"] == "u"
    assert validator.validate(token, timeout=0.1)["sub"] == "u"
    assert calls == [URI]


@pytest.fixture
def clock(monkeypatch):
    from pyfly.security.oauth2 import _jwks, resource_server

    now = [0.0]
    fake = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(_jwks, "time", fake)
    monkeypatch.setattr(resource_server, "time", fake)
    return now


@pytest.mark.parametrize("stage", ["fetch", "key_parse"])
def test_late_refresh_never_publishes_keys(stage, clock, monkeypatch):
    calls = []
    original = jwt.PyJWK.from_dict

    def fetch(uri, **kwargs):
        calls.append(kwargs["timeout"])
        if stage == "fetch" and len(calls) == 1:
            clock[0] += 1
        return BODY

    def parse(*args, **kwargs):
        key = original(*args, **kwargs)
        if stage == "key_parse" and len(calls) == 1:
            clock[0] += 1
        return key

    monkeypatch.setattr(jwt.PyJWK, "from_dict", parse)
    validator = JWKSTokenValidator(URI, jwks_fetcher=fetch, jwks_min_refresh_seconds=0.1)
    with pytest.raises(SecurityException):
        validator.validate(_mint({}), timeout=0.5)
    clock[0] += 0.2
    assert validator.validate(_mint({}), timeout=2)["exp"]
    assert len(calls) == 2
    assert calls[0] <= 0.5


def test_caller_budget_includes_jwt_verification(clock, monkeypatch):
    decode = jwt.decode
    calls = []

    def slow_decode(*args, **kwargs):
        payload = decode(*args, **kwargs)
        clock[0] += 1
        return payload

    monkeypatch.setattr(jwt, "decode", slow_decode)
    validator = JWKSTokenValidator(URI, jwks_fetcher=lambda uri, **kwargs: calls.append(uri) or BODY)
    with pytest.raises(SecurityException):
        validator.validate(_mint({}), timeout=0.5)
    assert len(calls) == 1


def test_failed_refresh_never_returns_stale_keys_after_original_expiry(clock):
    calls = []

    def fetch(uri, **kwargs):
        calls.append(uri)
        if len(calls) > 1:
            raise RuntimeError("outage")
        return BODY

    validator = JWKSTokenValidator(URI, jwks_fetcher=fetch, jwks_cache_seconds=1, jwks_min_refresh_seconds=0.1)
    token = _mint({"sub": "u"})
    assert validator.validate(token, timeout=1)["sub"] == "u"
    clock[0] = 0.5
    with pytest.raises(SecurityException):
        validator.validate(_mint({}, kid="unknown"), timeout=1)
    assert validator.validate(token, timeout=1)["sub"] == "u"
    clock[0] = 1.1
    with pytest.raises(SecurityException):
        validator.validate(token, timeout=1)
    assert len(calls) == 3


def test_default_fetch_ignores_ambient_proxy_settings(monkeypatch, jwks_endpoint):  # noqa: F811
    uri, _, state = jwks_endpoint
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("all_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    assert JWKSTokenValidator(uri).validate(_mint({}))["exp"]
    assert state.requests == 1


def test_default_http_open_uses_budget_remaining_after_transport_setup(clock, monkeypatch):
    from pyfly.security.oauth2 import _jwks

    timeouts = []

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, size):
            return b""

    class Opener:
        def open(self, request, *, timeout):
            timeouts.append(timeout)
            return Response()

    def build(*handlers):
        clock[0] += 0.25
        return Opener()

    monkeypatch.setattr(_jwks.urllib.request, "build_opener", build)
    assert _jwks._fetch_bytes(URI, timeout=0.5, max_bytes=100) == b""
    assert timeouts == [0.25]
