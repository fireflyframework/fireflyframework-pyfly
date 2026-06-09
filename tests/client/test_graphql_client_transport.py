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
"""Real-transport tests for GraphQLClient and SoapClient using respx (no Docker).

Both clients create a fresh httpx.AsyncClient per call, so respx global mock
intercepts them cleanly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from httpx import Request, Response

from pyfly.client.protocols.graphql_client import GraphQLClient
from pyfly.client.protocols.soap_client import SoapClient

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_graphql_execute_posts_correct_envelope() -> None:
    """execute() POSTs {query, variables, operationName} and returns data."""
    endpoint = "https://gql.test/graphql"
    client = GraphQLClient(endpoint, timeout=5.0)

    captured_body: dict[str, Any] = {}

    def capture(request: Request, **kwargs: Any) -> Response:
        captured_body.update(json.loads(request.content))
        return Response(200, json={"data": {"user": {"id": "1"}}})

    respx.post(endpoint).mock(side_effect=capture)

    result = await client.execute(
        "{ user { id } }",
        variables={"userId": "1"},
        operation_name="GetUser",
    )

    assert result == {"user": {"id": "1"}}
    assert captured_body["query"] == "{ user { id } }"
    assert captured_body["variables"] == {"userId": "1"}
    assert captured_body["operationName"] == "GetUser"


@respx.mock
@pytest.mark.asyncio
async def test_graphql_execute_omits_none_fields() -> None:
    """When variables / operation_name are None they are omitted from the payload."""
    endpoint = "https://gql.test/graphql"
    client = GraphQLClient(endpoint, timeout=5.0)

    captured_body: dict[str, Any] = {}

    def capture(request: Request, **kwargs: Any) -> Response:
        captured_body.update(json.loads(request.content))
        return Response(200, json={"data": {"ping": True}})

    respx.post(endpoint).mock(side_effect=capture)
    await client.execute("{ ping }")

    assert "variables" not in captured_body
    assert "operationName" not in captured_body


@respx.mock
@pytest.mark.asyncio
async def test_graphql_execute_raises_on_errors() -> None:
    """A response containing 'errors' key raises RuntimeError."""
    endpoint = "https://gql.test/graphql"
    client = GraphQLClient(endpoint, timeout=5.0)

    respx.post(endpoint).mock(return_value=Response(200, json={"errors": [{"message": "Not found"}]}))

    with pytest.raises(RuntimeError, match="GraphQL errors"):
        await client.execute("{ missing }")


# ---------------------------------------------------------------------------
# SOAP
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_soap_call_wraps_body_in_envelope() -> None:
    """call() wraps body_xml in SOAP envelope and POSTs to endpoint."""
    endpoint = "https://soap.test/service"
    client = SoapClient(endpoint, soap_action="DoThing", timeout=5.0)

    captured_content: list[str] = []

    def capture(request: Request, **kwargs: Any) -> Response:
        captured_content.append(request.content.decode())
        return Response(200, text="<ok/>")

    respx.post(endpoint).mock(side_effect=capture)
    result = await client.call("<GetFoo><id>42</id></GetFoo>")

    assert result == "<ok/>"
    body = captured_content[0]
    assert "soap:Envelope" in body
    assert "soap:Body" in body
    assert "<GetFoo><id>42</id></GetFoo>" in body


@respx.mock
@pytest.mark.asyncio
async def test_soap_call_sends_soap_action_header() -> None:
    """SOAPAction header is forwarded when set."""
    endpoint = "https://soap.test/service"
    client = SoapClient(endpoint, soap_action="MyAction", timeout=5.0)

    captured_headers: dict[str, str] = {}

    def capture(request: Request, **kwargs: Any) -> Response:
        captured_headers.update(dict(request.headers))
        return Response(200, text="<resp/>")

    respx.post(endpoint).mock(side_effect=capture)
    await client.call("<Payload/>")

    assert captured_headers.get("soapaction") == "MyAction"
    assert "text/xml" in captured_headers.get("content-type", "")
