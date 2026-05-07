# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Per-protocol service clients (REST is the existing httpx adapter; this
package adds SOAP, gRPC, GraphQL, WebSocket builders).

Mirrors ``org.fireflyframework.client``: every concrete client is exposed
through a builder that takes a base URL / target / endpoint, optional
``CircuitBreaker`` and ``RetryPolicy``, and returns an async client.
"""

from __future__ import annotations

from pyfly.client.protocols.graphql_client import GraphQLClient, GraphQLClientBuilder
from pyfly.client.protocols.grpc_client import GrpcClientBuilder
from pyfly.client.protocols.soap_client import SoapClient, SoapClientBuilder
from pyfly.client.protocols.websocket_client import WebSocketClient, WebSocketClientBuilder

__all__ = [
    "GraphQLClient",
    "GraphQLClientBuilder",
    "GrpcClientBuilder",
    "SoapClient",
    "SoapClientBuilder",
    "WebSocketClient",
    "WebSocketClientBuilder",
]
