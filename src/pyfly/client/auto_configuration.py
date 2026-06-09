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
"""HTTP client subsystem auto-configuration."""

from __future__ import annotations

from typing import Any

from pyfly.client.ports.outbound import HttpClientPort
from pyfly.client.post_processor import HttpClientBeanPostProcessor
from pyfly.client.protocols.grpc_client import GrpcClientBuilder
from pyfly.client.protocols.websocket_client import WebSocketClientBuilder
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
)
from pyfly.core.config import Config


@auto_configuration
@conditional_on_class("httpx")
@conditional_on_missing_bean(HttpClientPort)
class ClientAutoConfiguration:
    """Auto-configures the httpx HTTP client adapter."""

    @bean
    def http_client_adapter(self, config: Config) -> HttpClientPort:
        from datetime import timedelta

        from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

        timeout_s = int(config.get("pyfly.client.timeout", 30))
        return HttpxClientAdapter(timeout=timedelta(seconds=timeout_s))

    @bean
    def http_client_post_processor(self, config: Config) -> HttpClientBeanPostProcessor:
        from datetime import timedelta

        retry_cfg = config.get("pyfly.client.retry")
        cb_cfg = config.get("pyfly.client.circuit_breaker") or config.get("pyfly.client.circuit-breaker")

        default_retry: dict[str, Any] | None = None
        default_cb: dict[str, Any] | None = None

        if isinstance(retry_cfg, dict):
            default_retry = retry_cfg
        if isinstance(cb_cfg, dict):
            default_cb = cb_cfg

        # Build per-bean adapters with the configured timeout so declarative
        # clients honor pyfly.client.timeout (audit #15).
        timeout_s = int(config.get("pyfly.client.timeout", 30))

        def factory(base_url: str) -> Any:
            from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

            return HttpxClientAdapter(base_url=base_url, timeout=timedelta(seconds=timeout_s))

        return HttpClientBeanPostProcessor(
            factory,
            default_retry=default_retry,
            default_circuit_breaker=default_cb,
        )


@auto_configuration
@conditional_on_class("grpc")
@conditional_on_missing_bean(GrpcClientBuilder)
class GrpcClientAutoConfiguration:
    """Auto-configures a :class:`~pyfly.client.protocols.grpc_client.GrpcClientBuilder`.

    Active only when ``grpcio`` is installed.  Reads ``pyfly.client.grpc.target``
    from the config if set; otherwise returns a bare builder that the caller
    can further configure.
    """

    @bean
    def grpc_client_builder(self, config: Config) -> GrpcClientBuilder:
        builder = GrpcClientBuilder()
        target = config.get("pyfly.client.grpc.target")
        if isinstance(target, str) and target:
            builder = builder.with_target(target)
        return builder


@auto_configuration
@conditional_on_class("websockets")
@conditional_on_missing_bean(WebSocketClientBuilder)
class WebSocketClientAutoConfiguration:
    """Auto-configures a :class:`~pyfly.client.protocols.websocket_client.WebSocketClientBuilder`.

    Active only when ``websockets`` is installed.
    """

    @bean
    def websocket_client_builder(self) -> WebSocketClientBuilder:
        return WebSocketClientBuilder()
