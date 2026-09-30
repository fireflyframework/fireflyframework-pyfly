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
"""PyFly Client — REST + SOAP + gRPC + GraphQL + WebSocket clients with circuit breaker and retry."""

from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter
from pyfly.client.circuit_breaker import CircuitBreaker, CircuitState
from pyfly.client.declarative import delete, get, http_client, patch, post, put, service_client
from pyfly.client.exceptions import ResponseTooLargeException, UnsupportedContentEncodingException
from pyfly.client.ports.outbound import BoundedHttpClientPort, HttpClientPort
from pyfly.client.post_processor import HttpClientBeanPostProcessor
from pyfly.client.protocols import (
    GraphQLClient,
    GraphQLClientBuilder,
    GrpcClientBuilder,
    SoapClient,
    SoapClientBuilder,
    WebSocketClient,
    WebSocketClientBuilder,
)
from pyfly.client.retry import RetryPolicy

__all__ = [
    "BoundedHttpClientPort",
    "CircuitBreaker",
    "CircuitState",
    "GraphQLClient",
    "GraphQLClientBuilder",
    "GrpcClientBuilder",
    "HttpClientBeanPostProcessor",
    "HttpClientPort",
    "HttpxClientAdapter",
    "ResponseTooLargeException",
    "UnsupportedContentEncodingException",
    "RetryPolicy",
    "SoapClient",
    "SoapClientBuilder",
    "WebSocketClient",
    "WebSocketClientBuilder",
    "delete",
    "get",
    "http_client",
    "patch",
    "post",
    "put",
    "service_client",
]
