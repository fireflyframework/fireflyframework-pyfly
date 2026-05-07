# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""gRPC client builder — wraps the official ``grpcio`` library when present."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GrpcClientBuilder:
    """Build an async gRPC channel + stub.

    Use :meth:`channel` to obtain an ``aio.Channel``; pass that to your
    generated stub class. We deliberately do not depend on a specific
    protobuf-generated stub here.
    """

    target: str = ""
    secure: bool = False
    compression: str | None = None
    options: list[tuple[str, Any]] | None = None

    def with_target(self, value: str) -> GrpcClientBuilder:
        self.target = value
        return self

    def secured(self, value: bool = True) -> GrpcClientBuilder:
        self.secure = value
        return self

    def with_option(self, name: str, value: Any) -> GrpcClientBuilder:
        if self.options is None:
            self.options = []
        self.options.append((name, value))
        return self

    def channel(self) -> Any:
        if not self.target:
            msg = "GrpcClientBuilder requires a target"
            raise ValueError(msg)
        try:
            from grpc import aio, ssl_channel_credentials  # type: ignore[import-not-found]
        except ImportError as exc:  # noqa: BLE001
            msg = "GrpcClientBuilder requires grpcio — `pip install grpcio`"
            raise ImportError(msg) from exc
        if self.secure:
            return aio.secure_channel(self.target, ssl_channel_credentials(), options=self.options or [])
        return aio.insecure_channel(self.target, options=self.options or [])
