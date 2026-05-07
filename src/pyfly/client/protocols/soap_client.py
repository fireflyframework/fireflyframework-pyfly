# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""SOAP client — minimalist envelope-builder + httpx-based transport.

Production deployments should switch to ``zeep`` for full WSDL support; this
client covers the 80% case (no schema, just envelope construction)."""

from __future__ import annotations

from dataclasses import dataclass, field

_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Header/>"
    "<soap:Body>{body}</soap:Body>"
    "</soap:Envelope>"
)


class SoapClient:
    """Async SOAP 1.1 client backed by :mod:`httpx`."""

    def __init__(
        self,
        endpoint: str,
        *,
        soap_action: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._endpoint = endpoint
        self._soap_action = soap_action
        self._headers = {
            "Content-Type": "text/xml; charset=utf-8",
            **(headers or {}),
        }
        if soap_action:
            self._headers["SOAPAction"] = soap_action
        self._timeout = timeout

    async def call(self, body_xml: str) -> str:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "SoapClient requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc

        envelope = _ENVELOPE.format(body=body_xml)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._endpoint, content=envelope, headers=self._headers)
            resp.raise_for_status()
            return resp.text


@dataclass
class SoapClientBuilder:
    endpoint: str = ""
    soap_action: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0

    def with_endpoint(self, value: str) -> SoapClientBuilder:
        self.endpoint = value
        return self

    def with_action(self, value: str) -> SoapClientBuilder:
        self.soap_action = value
        return self

    def with_header(self, name: str, value: str) -> SoapClientBuilder:
        self.headers[name] = value
        return self

    def with_timeout(self, seconds: float) -> SoapClientBuilder:
        self.timeout = seconds
        return self

    def build(self) -> SoapClient:
        if not self.endpoint:
            msg = "SoapClientBuilder requires an endpoint"
            raise ValueError(msg)
        return SoapClient(
            endpoint=self.endpoint,
            soap_action=self.soap_action,
            headers=self.headers,
            timeout=self.timeout,
        )
