# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
import importlib.util


def test_optional_capability_description_never_calls_an_unknown_provider():
    assert importlib.util.find_spec("pyfly.notifications.capabilities") is not None, (
        "Native mail readiness needs explicit transport capabilities"
    )
    from pyfly.notifications.capabilities import EmailProviderCapabilities, email_capabilities
    from pyfly.notifications.providers.dummy import DummyEmailProvider
    from pyfly.notifications.providers.smtp import SmtpEmailProvider

    assert not email_capabilities(DummyEmailProvider()).real_delivery
    assert not email_capabilities(object()).real_delivery
    assert email_capabilities(SmtpEmailProvider("localhost")).inline_cid
    assert email_capabilities(SmtpEmailProvider("localhost")).real_delivery
    assert not email_capabilities(SmtpEmailProvider("localhost")).operation_lookup
    assert EmailProviderCapabilities().real_delivery is False
