# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""A syntactically valid caller header is never proof of a TLS peer."""

import pytest

from pyfly.web.adapters.starlette.filters.x509_filter import X509AuthenticationFilter
from tests.security.test_x509 import _call_next, _cert, _request


@pytest.mark.asyncio
async def test_default_rejects_forged_forwarded_certificate():
    request = _request(_cert("administrator"))
    response = await X509AuthenticationFilter().do_filter(request, _call_next)
    assert response.status_code == 401
    assert not getattr(request.state, "security_context", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [False, True])
async def test_host_must_validate_authenticated_ingress_provenance(trusted):
    request = _request(_cert("alice"))
    filter = X509AuthenticationFilter(trusted_ingress=lambda req: trusted)
    response = await filter.do_filter(request, _call_next)
    assert response.status_code == (200 if trusted else 401)
    if trusted:
        assert request.state.security_context.user_id == "alice"


@pytest.mark.asyncio
async def test_duplicate_certificate_header_rejected_even_from_trusted_ingress():
    request = _request(_cert("alice"))
    request.scope["headers"].append(request.scope["headers"][0])
    filter = X509AuthenticationFilter(trusted_ingress=lambda req: True)
    assert (await filter.do_filter(request, _call_next)).status_code == 401
