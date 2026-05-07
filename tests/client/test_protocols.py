# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the new client protocol builders (SOAP / gRPC / GraphQL / WebSocket)."""

from __future__ import annotations

import pytest

from pyfly.client.protocols import (
    GraphQLClient,
    GraphQLClientBuilder,
    GrpcClientBuilder,
    SoapClient,
    SoapClientBuilder,
    WebSocketClient,
    WebSocketClientBuilder,
)


def test_soap_builder_assembles_client() -> None:
    client = (
        SoapClientBuilder()
        .with_endpoint("https://soap.example.com/svc")
        .with_action("GetThing")
        .with_header("X-Auth", "abc")
        .build()
    )
    assert isinstance(client, SoapClient)
    assert client._endpoint == "https://soap.example.com/svc"
    assert client._headers["SOAPAction"] == "GetThing"


def test_soap_builder_requires_endpoint() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        SoapClientBuilder().build()


def test_graphql_builder_assembles_client() -> None:
    client = (
        GraphQLClientBuilder()
        .with_endpoint("https://api.example.com/graphql")
        .with_header("Authorization", "Bearer x")
        .with_timeout(5.0)
        .build()
    )
    assert isinstance(client, GraphQLClient)
    assert client._headers == {"Authorization": "Bearer x"}
    assert client._timeout == 5.0


def test_grpc_builder_requires_target() -> None:
    with pytest.raises(ValueError, match="target"):
        GrpcClientBuilder().channel()


def test_websocket_builder_assembles_client() -> None:
    client = (
        WebSocketClientBuilder().with_url("wss://example.com/ws").with_header("Origin", "https://example.com").build()
    )
    assert isinstance(client, WebSocketClient)
    assert client._url == "wss://example.com/ws"
    assert client._headers == {"Origin": "https://example.com"}


def test_grpc_builder_chains_options() -> None:
    builder = (
        GrpcClientBuilder()
        .with_target("localhost:50051")
        .secured(False)
        .with_option("grpc.max_receive_message_length", 1024)
    )
    assert builder.target == "localhost:50051"
    assert builder.options == [("grpc.max_receive_message_length", 1024)]
