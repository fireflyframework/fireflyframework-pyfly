# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Configuration mistakes and diagnostic reprs must not impersonate delivery."""

from __future__ import annotations

import pytest

from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.notifications.auto_configuration import NotificationsAutoConfiguration
from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus, NotificationResult
from pyfly.notifications.ports import EmailProvider
from pyfly.notifications.services import DefaultEmailService


def test_explicit_unknown_provider_never_falls_back_to_dummy_sent():
    with pytest.raises(ValueError):
        NotificationsAutoConfiguration().email_provider(
            Config({"pyfly": {"notifications": {"email": {"provider": "unconfigured-transport"}}}})
        )


@pytest.mark.parametrize(
    "value",
    [
        Attachment("synthetic-private-filename", "text/plain", b"synthetic-private-bytes"),
        EmailMessage(
            to=["synthetic-private-recipient@example.test"],
            body_text="synthetic-private-body",
            headers={"X-Private": "synthetic-private-header"},
            template_data={"private": "synthetic-private-template"},
        ),
        NotificationResult("owned", "smtp", EmailStatus.FAILED, error="synthetic-private-error"),
    ],
)
def test_payload_values_do_not_appear_in_default_representations(value):
    assert "synthetic-private" not in repr(value)


async def test_custom_port_binding_overrides_an_unrecognized_configured_provider():
    class Custom:
        name = "owned-custom"

        async def send(self, message):
            return NotificationResult(message.id, self.name, EmailStatus.SENT)

    provider = Custom()
    context = ApplicationContext(
        Config({"pyfly": {"notifications": {"enabled": True, "email": {"provider": "unconfigured-transport"}}}})
    )
    context.container.register_instance(EmailProvider, provider, name="custom_email")
    await context.start()
    try:
        service = context.get_bean(DefaultEmailService)
        assert service._provider is provider
    finally:
        await context.stop()


def test_azure_configuration_requires_explicit_identity_and_preserves_operator_sender():
    from pyfly.notifications import AzureCommunicationEmailProvider

    values = {
        "pyfly": {
            "notifications": {
                "email": {
                    "provider": "azure-communication-email",
                    "from": "sender@example.test",
                    "azure": {
                        "endpoint": "https://owned-fixture.communication.azure.com",
                        "credential-mode": "managed-identity",
                    },
                }
            }
        }
    }
    provider = NotificationsAutoConfiguration().email_provider(Config(values))
    assert isinstance(provider, AzureCommunicationEmailProvider)
    assert provider._client is None
    values["pyfly"]["notifications"]["email"]["azure"]["credential-mode"] = "default-chain"
    with pytest.raises(ValueError):
        NotificationsAutoConfiguration().email_provider(Config(values))


def test_inline_identifier_is_private_in_attachment_repr():
    attachment = Attachment("logo.png", "image/png", b"fixture", "synthetic-private-cid", True)
    assert "synthetic-private" not in repr(attachment)
