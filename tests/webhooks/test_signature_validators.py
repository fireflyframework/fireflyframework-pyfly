# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Unit tests for provider-specific webhook signature validators.

No Docker required — all assertions use real HMAC computations so the expected
values are derived from the same algorithm under test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _stripe_header(secret: bytes, body: bytes, timestamp: int | None = None, *, bad_hmac: bool = False) -> str:
    """Build a synthetic ``Stripe-Signature`` header value."""
    t = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{t}.{body.decode('utf-8')}".encode()
    mac = hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()
    if bad_hmac:
        mac = "0" * len(mac)
    return f"t={t},v1={mac}"


def _github_header(secret: bytes, body: bytes) -> str:
    """Build a synthetic ``X-Hub-Signature-256`` header value."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _twilio_header(auth_token: bytes, url: str, params: dict[str, str]) -> str:
    """Build a synthetic ``X-Twilio-Signature`` header value."""
    signed = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    mac = hmac.new(auth_token, signed.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(mac).decode("ascii")


# ---------------------------------------------------------------------------
# StripeSignatureValidator
# ---------------------------------------------------------------------------


def test_stripe_valid() -> None:
    from pyfly.webhooks.signature import StripeSignatureValidator

    secret = "whsec_test_stripe"
    body = b'{"type":"payment_intent.succeeded"}'
    header = _stripe_header(secret.encode(), body)
    validator = StripeSignatureValidator(secret, tolerance_seconds=300)
    assert validator.is_valid(body=body, signature=header) is True


def test_stripe_invalid_hmac() -> None:
    from pyfly.webhooks.signature import StripeSignatureValidator

    secret = "whsec_test_stripe"
    body = b'{"type":"payment_intent.succeeded"}'
    header = _stripe_header(secret.encode(), body, bad_hmac=True)
    validator = StripeSignatureValidator(secret, tolerance_seconds=300)
    assert validator.is_valid(body=body, signature=header) is False


def test_stripe_replay_expired() -> None:
    from pyfly.webhooks.signature import StripeSignatureValidator

    secret = "whsec_test_stripe"
    body = b'{"type":"charge.failed"}'
    old_timestamp = int(time.time()) - 400  # 400 s ago — beyond 300 s tolerance
    header = _stripe_header(secret.encode(), body, timestamp=old_timestamp)
    validator = StripeSignatureValidator(secret, tolerance_seconds=300)
    assert validator.is_valid(body=body, signature=header) is False


def test_stripe_malformed_header_returns_false() -> None:
    from pyfly.webhooks.signature import StripeSignatureValidator

    validator = StripeSignatureValidator("any_secret")
    assert validator.is_valid(body=b"body", signature=None) is False
    assert validator.is_valid(body=b"body", signature="not_a_valid_header") is False
    assert validator.is_valid(body=b"body", signature="v1=abc") is False  # missing t=


# ---------------------------------------------------------------------------
# GitHubSignatureValidator
# ---------------------------------------------------------------------------


def test_github_valid() -> None:
    from pyfly.webhooks.signature import GitHubSignatureValidator

    secret = "github_webhook_secret"
    body = b'{"action":"opened"}'
    header = _github_header(secret.encode(), body)
    validator = GitHubSignatureValidator(secret)
    assert validator.is_valid(body=body, signature=header) is True


def test_github_invalid() -> None:
    from pyfly.webhooks.signature import GitHubSignatureValidator

    validator = GitHubSignatureValidator("correct_secret")
    assert validator.is_valid(body=b"payload", signature="sha256=" + "0" * 64) is False


def test_github_no_signature() -> None:
    from pyfly.webhooks.signature import GitHubSignatureValidator

    validator = GitHubSignatureValidator("any_secret")
    assert validator.is_valid(body=b"payload", signature=None) is False


# ---------------------------------------------------------------------------
# TwilioSignatureValidator
# ---------------------------------------------------------------------------


def test_twilio_valid() -> None:
    from pyfly.webhooks.signature import TwilioSignatureValidator

    auth_token = "twilio_auth_token_test"
    url = "https://example.com/webhooks/sms"
    params = {"Body": "Hello", "From": "+15551234567", "To": "+15559876543"}
    header = _twilio_header(auth_token.encode(), url, params)
    validator = TwilioSignatureValidator(auth_token)
    assert validator.is_valid(url=url, params=params, signature=header) is True


def test_twilio_invalid_signature() -> None:
    from pyfly.webhooks.signature import TwilioSignatureValidator

    validator = TwilioSignatureValidator("correct_token")
    assert (
        validator.is_valid(
            url="https://example.com/webhook",
            params={"Body": "Hi"},
            signature="AAAA",
        )
        is False
    )


def test_twilio_missing_signature() -> None:
    from pyfly.webhooks.signature import TwilioSignatureValidator

    validator = TwilioSignatureValidator("any_token")
    assert validator.is_valid(url="https://example.com/", params={}, signature=None) is False


def test_twilio_different_params_order_same_result() -> None:
    """Params are sorted before hashing, so order must not matter."""
    from pyfly.webhooks.signature import TwilioSignatureValidator

    auth_token = "sort_test_token"
    url = "https://example.com/twilio"
    params = {"Z": "last", "A": "first", "M": "middle"}
    header = _twilio_header(auth_token.encode(), url, params)
    validator = TwilioSignatureValidator(auth_token)
    # Supply params in a different iteration order
    shuffled = {"M": "middle", "Z": "last", "A": "first"}
    assert validator.is_valid(url=url, params=shuffled, signature=header) is True
