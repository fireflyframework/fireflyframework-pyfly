# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus
from pyfly.notifications.providers.resend import ResendEmailProvider
from pyfly.notifications.providers.sendgrid import SendGridEmailProvider
from pyfly.notifications.services import DefaultEmailService
from tests.notifications.test_sendgrid_behavior import FakeHttpClient, FakeResponse


@pytest.mark.parametrize("provider", [SendGridEmailProvider("synthetic-key"), ResendEmailProvider("synthetic-key")])
async def test_native_http_cid_and_message_id_are_preserved(provider):
    client = FakeHttpClient(
        FakeResponse(202, {"id": "provider-operation"}, headers={"X-Message-Id": "provider-operation"})
    )
    provider._http = client
    message = EmailMessage(
        id="stable-message",
        sender="sender@example.test",
        to=["recipient@example.test"],
        body_html='<img src="cid:brand-logo">',
        attachments=[Attachment("logo.png", "image/png", b"fixture", content_id="brand-logo", inline=True)],
    )
    assert (await provider.send(message)).status == EmailStatus.SENT
    payload = client.calls[0][2]["json"]
    assert payload["headers"]["Message-ID"]
    item = payload["attachments"][0]
    if provider.name == "sendgrid":
        assert item["content_id"] == "brand-logo" and item["disposition"] == "inline"
    else:
        assert item["content_id"] == "brand-logo"


@pytest.mark.parametrize("provider", [SendGridEmailProvider("synthetic-key"), ResendEmailProvider("synthetic-key")])
async def test_native_http_rejections_do_not_return_private_provider_body(provider):
    provider._http = FakeHttpClient(FakeResponse(400, text="synthetic-private-upstream-body"))
    result = await provider.send(EmailMessage(sender="sender@example.test", to=["recipient@example.test"]))
    assert result.status == EmailStatus.FAILED and "synthetic-private" not in (result.error or "")


async def test_unclassified_provider_exception_is_uncertain_and_redacted():
    class Provider:
        name = "owned"

        async def send(self, message):
            raise RuntimeError("synthetic-private-token")

    result = await DefaultEmailService(Provider()).send(EmailMessage())
    assert result.status == EmailStatus.UNKNOWN
    assert result.error == "EMAIL_SEND_OUTCOME_UNKNOWN"


async def test_local_preparation_error_does_not_dispatch_or_expose_template_data():
    class Provider:
        name = "owned"

        async def send(self, message):
            pytest.fail("A preparation failure cannot dispatch")

    class Template:
        async def render(self, *args):
            raise ValueError("synthetic-private-template")

    result = await DefaultEmailService(Provider(), template_engine=Template()).send(EmailMessage(template_id="owned"))
    assert result.status == EmailStatus.FAILED and result.error == "EMAIL_PREPARATION_FAILED"


@pytest.mark.parametrize("kind", [SendGridEmailProvider, ResendEmailProvider])
def test_http_email_credentials_cannot_use_plaintext_or_userinfo_endpoint(kind):
    for endpoint in ["http://localhost", "https://user:secret@example.test"]:
        with pytest.raises(ValueError):
            kind("synthetic-key", api_base=endpoint)


async def test_diagnostic_failure_cannot_turn_known_acceptance_into_a_send_retry():
    from pyfly.notifications import DummyEmailProvider

    class Counter:
        def labels(self, **kwargs):
            return self

        def inc(self):
            raise RuntimeError("diagnostic unavailable")

    class Metrics:
        def counter(self, *args, **kwargs):
            return Counter()

    provider = DummyEmailProvider()
    result = await DefaultEmailService(provider, metrics=Metrics()).send(EmailMessage())
    assert result.status == EmailStatus.SENT and len(provider.sent) == 1
