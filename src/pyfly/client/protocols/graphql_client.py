# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""GraphQL client — POSTs operations to a single endpoint over HTTPS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class GraphQLClient:
    """Thin async GraphQL client that wraps :mod:`httpx`."""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._endpoint = endpoint
        self._headers = dict(headers or {})
        self._timeout = timeout

    async def execute(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "GraphQLClient requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc

        body: dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables
        if operation_name is not None:
            body["operationName"] = operation_name
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._endpoint, json=body, headers=self._headers)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            payload: dict[str, Any] = data.get("data") or {}
            return payload


@dataclass
class GraphQLClientBuilder:
    """Fluent builder for :class:`GraphQLClient`."""

    endpoint: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0

    def with_endpoint(self, value: str) -> GraphQLClientBuilder:
        self.endpoint = value
        return self

    def with_header(self, name: str, value: str) -> GraphQLClientBuilder:
        self.headers[name] = value
        return self

    def with_timeout(self, seconds: float) -> GraphQLClientBuilder:
        self.timeout = seconds
        return self

    def build(self) -> GraphQLClient:
        if not self.endpoint:
            msg = "GraphQLClientBuilder requires an endpoint"
            raise ValueError(msg)
        return GraphQLClient(endpoint=self.endpoint, headers=self.headers, timeout=self.timeout)
