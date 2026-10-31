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

from datetime import timedelta
from ssl import CERT_REQUIRED, SSLContext
from typing import Annotated, Any

from pyfly.client.ports.outbound import HttpClientPort, StreamingHttpClientPort
from pyfly.client.post_processor import HttpClientBeanPostProcessor
from pyfly.client.protocols.grpc_client import GrpcClientBuilder
from pyfly.client.protocols.websocket_client import WebSocketClientBuilder
from pyfly.config.properties.client import ClientTlsProperties
from pyfly.container.bean import Qualifier, bean
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
    def http_client_adapter(
        self,
        config: Config,
        ssl_context: Annotated[SSLContext | None, Qualifier("http_client_ssl_context")] = None,
    ) -> HttpClientPort:
        from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

        return HttpxClientAdapter(**self._adapter_options(config, ssl_context))

    @staticmethod
    def _adapter_options(config: Config, ssl_context: SSLContext | None = None) -> dict[str, Any]:
        tls = ClientTlsProperties(
            ca_file=config.get("pyfly.client.tls.ca-file"),
            certfile=config.get("pyfly.client.tls.certfile"),
            keyfile=config.get("pyfly.client.tls.keyfile"),
        )
        configured_tls = any((tls.ca_file, tls.certfile, tls.keyfile))
        if ssl_context is not None:
            if configured_tls:
                raise ValueError("Configure TLS files or the named SSLContext bean, not both")
            if not ssl_context.check_hostname or ssl_context.verify_mode != CERT_REQUIRED:
                raise ValueError("Native client SSLContext requires certificate and hostname verification")
        options: dict[str, Any] = {
            "timeout": timedelta(seconds=float(config.get("pyfly.client.timeout", 30))),
            "trust_env": str(config.get("pyfly.client.trust-env", True)).lower() == "true",
            "follow_redirects": str(config.get("pyfly.client.follow-redirects", False)).lower() == "true",
            "retries": config.get("pyfly.client.transport-retries", 0),
            "headers": config.get("pyfly.client.headers"),
            "tls": tls if configured_tls else None,
        }
        if ssl_context is not None:
            options["verify"] = ssl_context
        return options

    @bean
    def streaming_http_client(self, client: HttpClientPort) -> StreamingHttpClientPort:
        if not isinstance(client, StreamingHttpClientPort):
            raise TypeError("Configured native HTTP adapter lacks streaming support")
        return client

    @bean
    def http_client_post_processor(
        self,
        config: Config,
        ssl_context: Annotated[SSLContext | None, Qualifier("http_client_ssl_context")] = None,
    ) -> HttpClientBeanPostProcessor:
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
        options = self._adapter_options(config, ssl_context)

        def factory(base_url: str) -> Any:
            from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

            return HttpxClientAdapter(base_url=base_url, **options)

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
