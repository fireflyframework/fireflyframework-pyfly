"""Exact bytes and explicit trust at webhook boundaries."""

import hashlib
import hmac
import time

import pytest
from starlette.requests import Request

from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.params import Body
from pyfly.webhooks.processor import WebhookProcessor
from pyfly.webhooks.signature import HmacSignatureValidator, NoOpSignatureValidator, StripeSignatureValidator


@pytest.mark.parametrize("payload", [b"", b"\xff\x00\x80\r\n", b'{ "a": 1 }'])
async def test_body_bytes_preserves_original(payload):
    async def handler(body: Body[bytes]):
        pass

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "headers": []}, receive)
    assert (await ParameterResolver(handler).resolve(request))["body"] == payload


def test_stripe_signs_original_binary_bytes():
    timestamp = str(int(time.time()))
    body = b"\xff\x80\x00"
    digest = hmac.new(b"secret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert StripeSignatureValidator("secret").is_valid(body=body, signature=f"t={timestamp},v1={digest}")


async def test_unknown_source_denied():
    with pytest.raises(ValueError, match="validator"):
        await WebhookProcessor().process(source="unknown", raw_body=b"{}", headers={})


async def test_explicit_unsigned_source_is_possible():
    processor = WebhookProcessor(signature_validators={"trusted": NoOpSignatureValidator()})
    assert (await processor.process(source="trusted", raw_body=b"{}", headers={})).source == "trusted"


async def test_processor_size_cap_before_validation():
    processor = WebhookProcessor(max_body_bytes=2)
    with pytest.raises(ValueError, match="limit"):
        await processor.process(source="unknown", raw_body=b"123", headers={})


async def test_signature_header_case_and_ambiguity():
    processor = WebhookProcessor(signature_validators={"test": HmacSignatureValidator("secret")})
    signature = "sha256=" + hmac.new(b"secret", b"{}", hashlib.sha256).hexdigest()
    await processor.process(source="test", raw_body=b"{}", headers={"x-signature": signature})
    with pytest.raises(ValueError, match="ambiguous"):
        await processor.process(
            source="test", raw_body=b"{}", headers={"x-signature": signature, "X-Signature": signature}
        )


def test_stripe_non_ascii_timestamp_is_invalid_without_raising():
    timestamp = str(int(time.time())).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    assert not StripeSignatureValidator("secret").is_valid(body=b"{}", signature=f"t={timestamp},v1=invalid")
