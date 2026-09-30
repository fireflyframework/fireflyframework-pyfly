"""Async borrowed JWKS retrieval preserves deadline, cancellation and key bounds."""

import asyncio
import json
import traceback

import jwt
import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2 import JWKSTokenValidator
from tests.security.test_oauth2_resource_server import KEY1, KEY2, _jwk, _mint

URI = "https://fixed.example/keys"
BODY = json.dumps({"keys": [_jwk(KEY1.public_key(), "k1")]}).encode()
BODY2 = json.dumps({"keys": [_jwk(KEY2.public_key(), "k2")]}).encode()


async def test_public_protocol_accepts_async_structural_callable_and_fixed_uri():
    from pyfly.security.oauth2 import AsyncJWKSFetcher

    calls = []
    caller = asyncio.current_task()

    async def fetch(uri: str, *, timeout: float, max_bytes: int) -> bytes:
        assert asyncio.current_task() is caller
        calls.append((uri, timeout, max_bytes))
        return BODY

    fetcher: AsyncJWKSFetcher = fetch
    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetcher, jwks_timeout=5, jwks_max_bytes=1000)
    token = jwt.encode(
        {"exp": 9999999999, "sub": "u"},
        KEY1,
        algorithm="RS256",
        headers={"kid": "k1", "jku": "https://untrusted.example/keys", "x5u": "file:///secret"},
    )
    assert (await validator.validate_async(token, timeout=2))["sub"] == "u"
    assert (await validator.validate_async(token, timeout=2))["sub"] == "u"
    assert len(calls) == 1
    assert calls[0][0] == URI
    assert 0 < calls[0][1] <= 2
    assert calls[0][2] == 1000


async def test_async_validation_requires_explicit_async_fetcher_without_sync_fallback():
    def fetch(uri, **kwargs):
        pytest.fail("Async validation must not call a synchronous fetcher")

    validator = JWKSTokenValidator(URI, jwks_fetcher=fetch)
    with pytest.raises(ValueError):
        await validator.validate_async(_mint({}))


@pytest.mark.parametrize("budget", [0, -1, True, float("nan"), float("inf"), "1", 10**1000])
async def test_invalid_async_budget_fails_before_fetch(budget):
    async def fetch(uri, **kwargs):
        pytest.fail("Invalid budgets must fail before fetch")

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch)
    with pytest.raises(ValueError):
        await validator.validate_async(_mint({}), timeout=budget)


@pytest.mark.parametrize(
    "document", [b"not json", b"[]", b'{"keys":null}', b'{"keys":[null]}', b'{"keys":[]}', {}, "{}"]
)
async def test_async_adapter_cannot_bypass_document_validation(document):
    async def fetch(uri, **kwargs):
        return document

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch)
    with pytest.raises(SecurityException) as caught:
        await validator.validate_async(_mint({}), timeout=1)
    assert caught.value.code == "INVALID_TOKEN"


@pytest.mark.parametrize("limit", ["bytes", "keys"])
async def test_async_adapter_cannot_bypass_byte_or_key_count_limit(limit):
    options = {"jwks_max_bytes": len(BODY) - 1} if limit == "bytes" else {"jwks_max_keys": 1}
    document = (
        BODY
        if limit == "bytes"
        else json.dumps({"keys": [_jwk(KEY1.public_key(), "k1"), _jwk(KEY2.public_key(), "k2")]}).encode()
    )

    async def fetch(uri, **kwargs):
        return document

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch, **options)
    with pytest.raises(SecurityException):
        await validator.validate_async(_mint({}), timeout=1)


@pytest.mark.parametrize(
    "change", [{"kid": "other"}, {"alg": "RS384"}, {"n": ""}, {"use": "enc"}, {"key_ops": ["sign"]}]
)
async def test_async_adapter_requires_matching_valid_signing_key(change):
    body = json.dumps({"keys": [{**_jwk(KEY1.public_key(), "k1"), **change}]}).encode()

    async def fetch(uri, **kwargs):
        return body

    with pytest.raises(SecurityException):
        await JWKSTokenValidator(URI, async_jwks_fetcher=fetch).validate_async(_mint({}), timeout=1)


async def test_async_fetch_failure_redacts_traceback_and_exception_chain():
    secret = "ASYNC-FETCHER-CREDENTIAL-SENTINEL"

    async def fetch(uri, **kwargs):
        raise RuntimeError(secret)

    with pytest.raises(SecurityException) as caught:
        await JWKSTokenValidator(URI, async_jwks_fetcher=fetch).validate_async(_mint({}), timeout=1)
    assert caught.value.code == "INVALID_TOKEN"
    assert secret not in str(caught.value) + repr(caught.value) + "".join(traceback.format_exception(caught.value))
    cause = caught.value.__cause__
    while cause is not None:
        assert secret not in str(cause)
        cause = cause.__cause__


@pytest.mark.parametrize("waiter_outcome", ["timeout", "cancel"])
async def test_async_waiter_deadline_or_cancel_does_not_cancel_refresh_owner(waiter_outcome):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fetch(uri, **kwargs):
        calls.append(uri)
        entered.set()
        await release.wait()
        return BODY

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch)
    token = _mint({"sub": "u"})
    owner = asyncio.create_task(validator.validate_async(token, timeout=1))
    await asyncio.wait_for(entered.wait(), 2)
    waiter = asyncio.create_task(validator.validate_async(token, timeout=0.02 if waiter_outcome == "timeout" else 1))
    try:
        if waiter_outcome == "timeout":
            with pytest.raises(SecurityException):
                await asyncio.wait_for(waiter, 2)
        else:
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert not owner.done()
        assert calls == [URI]
    finally:
        release.set()
        assert (await asyncio.wait_for(owner, 2))["sub"] == "u"
    assert (await validator.validate_async(token, timeout=0.1))["sub"] == "u"
    assert calls == [URI]


async def test_owner_cancellation_propagates_and_awaits_fetch_cleanup():
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    finished = asyncio.Event()

    async def fetch(uri, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            finished.set()

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch)
    owner = asyncio.create_task(validator.validate_async(_mint({}), timeout=1))
    await asyncio.wait_for(entered.wait(), 2)
    owner.cancel()
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not owner.done()
        assert not finished.is_set()
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 2)
    assert finished.is_set()


async def test_suppressed_timeout_return_is_rejected_without_publishing_keys():
    calls = []

    async def fetch(uri, **kwargs):
        calls.append(uri)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return BODY
        return BODY2

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch, jwks_min_refresh_seconds=0.001)
    with pytest.raises(SecurityException):
        await asyncio.wait_for(validator.validate_async(_mint({}), timeout=0.02), 2)
    await asyncio.sleep(0.005)
    # If the late first response were published, this would incorrectly hit k1.
    with pytest.raises(SecurityException):
        await validator.validate_async(_mint({}), timeout=1)
    assert len(calls) == 2
    token2 = _mint({"sub": "rotated"}, key=KEY2, kid="k2")
    assert (await validator.validate_async(token2, timeout=1))["sub"] == "rotated"
    assert len(calls) == 2


async def test_async_rotation_replaces_keys_and_failed_refresh_cannot_extend_original_expiry(monotonic_clock):
    document = BODY
    calls = []

    async def fetch(uri, **kwargs):
        calls.append(uri)
        if document is None:
            raise RuntimeError("outage-secret")
        return document

    validator = JWKSTokenValidator(
        URI,
        async_jwks_fetcher=fetch,
        jwks_cache_seconds=10,
        jwks_min_refresh_seconds=1,
    )
    token = _mint({"sub": "u"})
    assert (await validator.validate_async(token, timeout=1))["sub"] == "u"
    document = BODY2
    token2 = _mint({"sub": "rotated"}, key=KEY2, kid="k2")
    assert (await validator.validate_async(token2, timeout=1))["sub"] == "rotated"
    with pytest.raises(SecurityException):
        await validator.validate_async(token, timeout=1)
    document = None
    monotonic_clock[0] = 11
    with pytest.raises(SecurityException):
        await validator.validate_async(token2, timeout=1)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "claims,key",
    [({"iss": "https://wrong.example"}, KEY1), ({"aud": "wrong"}, KEY1), ({"exp": 1}, KEY1), ({}, KEY2)],
)
async def test_async_validation_keeps_signature_issuer_audience_and_expiry_checks(claims, key):
    async def fetch(uri, **kwargs):
        return BODY

    validator = JWKSTokenValidator(
        URI,
        async_jwks_fetcher=fetch,
        issuer="https://issuer.example",
        audiences=["api"],
        leeway=0,
    )
    token = _mint({"iss": "https://issuer.example", "aud": "api", **claims}, key=key)
    with pytest.raises(SecurityException) as caught:
        await validator.validate_async(token, timeout=1)
    assert caught.value.code == "INVALID_TOKEN"


async def test_refresh_timeout_applies_without_caller_budget():
    finished = asyncio.Event()

    async def fetch(uri, **kwargs):
        assert 0 < kwargs["timeout"] <= 0.02
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch, jwks_timeout=0.02)
    with pytest.raises(SecurityException):
        await asyncio.wait_for(validator.validate_async(_mint({})), 2)
    assert finished.is_set()


@pytest.fixture
def monotonic_clock(monkeypatch):
    from types import SimpleNamespace

    from pyfly.security.oauth2 import _jwks, resource_server

    now = [0.0]
    fake = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(_jwks, "time", fake)
    monkeypatch.setattr(resource_server, "time", fake)
    return now


async def test_failed_async_refresh_does_not_extend_previous_snapshot_expiry(monotonic_clock):
    calls = []

    async def fetch(uri, **kwargs):
        calls.append(uri)
        if len(calls) > 1:
            raise RuntimeError("outage")
        return BODY

    validator = JWKSTokenValidator(
        URI,
        async_jwks_fetcher=fetch,
        jwks_cache_seconds=1,
        jwks_min_refresh_seconds=0.1,
    )
    token = _mint({"sub": "u"})
    assert (await validator.validate_async(token, timeout=1))["sub"] == "u"
    monotonic_clock[0] = 0.5
    with pytest.raises(SecurityException):
        await validator.validate_async(_mint({}, kid="unknown"), timeout=1)
    assert (await validator.validate_async(token, timeout=1))["sub"] == "u"
    monotonic_clock[0] = 1.1
    with pytest.raises(SecurityException):
        await validator.validate_async(token, timeout=1)
    assert len(calls) == 3


@pytest.mark.parametrize("stage", ["fetch", "key_parse", "verify"])
async def test_async_budget_includes_fetch_parse_and_token_verification(stage, monotonic_clock, monkeypatch):
    calls = []
    parse = jwt.PyJWK.from_dict
    decode = jwt.decode

    async def fetch(uri, **kwargs):
        calls.append(uri)
        if stage == "fetch":
            monotonic_clock[0] += 1
        return BODY

    def parse_key(*args, **kwargs):
        result = parse(*args, **kwargs)
        if stage == "key_parse":
            monotonic_clock[0] += 1
        return result

    def verify(*args, **kwargs):
        result = decode(*args, **kwargs)
        if stage == "verify":
            monotonic_clock[0] += 1
        return result

    monkeypatch.setattr(jwt.PyJWK, "from_dict", parse_key)
    monkeypatch.setattr(jwt, "decode", verify)
    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch)
    with pytest.raises(SecurityException):
        await validator.validate_async(_mint({}), timeout=0.5)
    assert calls == [URI]


async def test_async_failure_does_not_retry_or_fall_back_to_sync_adapter():
    calls = []

    async def fetch(uri, **kwargs):
        calls.append(uri)
        raise RuntimeError("failure")

    def sync_fetch(uri, **kwargs):
        pytest.fail("Async failure must not fall back to synchronous egress")

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch, jwks_fetcher=sync_fetch)
    with pytest.raises(SecurityException):
        await validator.validate_async(_mint({}), timeout=1)
    assert calls == [URI]


async def test_short_refresh_timeout_cleanup_survives_longer_caller_deadline():
    finished = asyncio.Event()

    async def fetch(uri, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            finished.set()

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch, jwks_timeout=0.01)
    with pytest.raises(SecurityException):
        await asyncio.wait_for(validator.validate_async(_mint({}), timeout=0.025), 2)
    assert finished.is_set(), "Caller deadline interrupted fetch cleanup after refresh timeout"


async def test_owner_cancellation_cleanup_survives_pending_refresh_timeout():
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def fetch(uri, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            finished.set()

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch, jwks_timeout=0.025)
    owner = asyncio.create_task(validator.validate_async(_mint({}), timeout=1))
    await asyncio.wait_for(entered.wait(), 2)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 2)
    assert finished.is_set(), "Refresh deadline interrupted cleanup after caller cancellation"


async def test_external_cancellation_after_owned_timeout_remains_cancellation():
    cleaning = asyncio.Event()

    async def fetch(uri, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await asyncio.Event().wait()

    validator = JWKSTokenValidator(URI, async_jwks_fetcher=fetch, jwks_timeout=0.01)
    owner = asyncio.create_task(validator.validate_async(_mint({}), timeout=1))
    await asyncio.wait_for(cleaning.wait(), 2)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 2)
    assert owner.cancelled()
