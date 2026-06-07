# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Pooled outbound HTTP client (v26.06.70) — wrapper + provider pooling/lifecycle."""

from __future__ import annotations

import pytest

from pyfly.client.pooled import PooledHttpClient


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pooled_wrapper_does_not_close_on_exit() -> None:
    fake = _FakeClient()
    async with PooledHttpClient(fake) as client:
        assert client is fake
    assert fake.closed is False  # the shared client stays open for reuse


@pytest.mark.asyncio
async def test_email_provider_pools_and_closes_client() -> None:
    from pyfly.notifications.providers.sendgrid import SendGridEmailProvider

    provider = SendGridEmailProvider(api_key="x")
    async with await provider._client() as c1:
        pass
    async with await provider._client() as c2:
        pass
    assert c1 is c2  # one pooled client reused across calls
    assert not c1.is_closed
    await provider.stop()
    assert c1.is_closed and provider._http is None


@pytest.mark.asyncio
async def test_ecm_adapter_pools_and_closes_client() -> None:
    from pyfly.ecm.adapters.docusign import DocuSignESignatureAdapter

    adapter = DocuSignESignatureAdapter(base_url="https://demo.docusign.net", account_id="a", access_token="t")
    c1 = (await adapter._client())._client
    c2 = (await adapter._client())._client
    assert c1 is c2
    await adapter.stop()
    assert c1.is_closed and adapter._http is None
