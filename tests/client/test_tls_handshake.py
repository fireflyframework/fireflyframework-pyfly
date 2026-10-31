# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Actual owned ephemeral TLS and mutual TLS, without an existing service."""

from __future__ import annotations

import asyncio
import ipaddress
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter
from pyfly.config.properties.client import ClientTlsProperties
from pyfly.config.properties.server import ServerProperties


@pytest.fixture
def certificates(tmp_path):
    def issue(name, *, issuer=None, client=False, expired=False):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer[0].subject if issuer else subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=-1 if expired else 2))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key((issuer[1] if issuer else key).public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=issuer is None,
                    crl_sign=issuer is None,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
        )
        if issuer is None:
            builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        else:
            builder = builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            builder = builder.add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            if not client:
                builder = builder.add_extension(
                    x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False
                )
        certificate = builder.sign(issuer[1] if issuer else key, hashes.SHA256())
        certfile, keyfile = tmp_path / (name + ".pem"), tmp_path / (name + ".key")
        certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        keyfile.chmod(0o600)
        return certificate, key, str(certfile), str(keyfile)

    ca = issue("ca")
    foreign = issue("foreign-ca")
    return {
        "ca": ca,
        "foreign-ca": foreign,
        "server": issue("server", issuer=ca),
        "valid": issue("valid", issuer=ca, client=True),
        "foreign": issue("foreign", issuer=foreign, client=True),
        "expired": issue("expired", issuer=ca, client=True, expired=True),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["uvicorn", "hypercorn"])
async def test_actual_mutual_tls_requires_current_trusted_client_identity(certificates, adapter, monkeypatch):
    ca, server = certificates["ca"], certificates["server"]
    props = ServerProperties(
        host="127.0.0.1",
        port=0,
        ssl_certfile=server[2],
        ssl_keyfile=server[3],
        ssl_ca_certs=ca[2],
        ssl_cert_reqs="required",
    )
    if adapter == "uvicorn":
        import uvicorn

        from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

        async def app(scope, receive, send):
            pass

        native = uvicorn.Config(app, **UvicornServerAdapter._build_kwargs("127.0.0.1", 0, "asyncio", props))
        native.load()
        context = native.ssl
    else:
        module = pytest.importorskip("hypercorn.asyncio")
        from pyfly.server.adapters.hypercorn.adapter import HypercornServerAdapter

        seen = []

        async def no_listener(app, config, **kwargs):
            seen.append(config)

        monkeypatch.setattr(module, "serve", no_listener)
        await HypercornServerAdapter().serve_async("inert:app", props)
        context = seen[0].create_ssl_context()
    handled = []

    async def respond(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            handled.append(True)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=context)
    try:
        url = f"https://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        for identity in ("valid", "none", "foreign", "expired"):
            item = certificates.get(identity)
            client = HttpxClientAdapter(
                trust_env=False,
                timeout=timedelta(seconds=2),
                tls=ClientTlsProperties(
                    ca_file=ca[2], certfile=item[2] if item else None, keyfile=item[3] if item else None
                ),
            )
            try:
                if identity == "valid":
                    assert (await client.request("GET", url)).status_code == 200
                else:
                    with pytest.raises(httpx.TransportError):
                        await client.request("GET", url)
            finally:
                await client.stop()
        assert len(handled) == 1
        client = HttpxClientAdapter(
            trust_env=False,
            tls=ClientTlsProperties(
                ca_file=certificates["foreign-ca"][2],
                certfile=certificates["valid"][2],
                keyfile=certificates["valid"][3],
            ),
        )
        try:
            with pytest.raises(httpx.ConnectError):
                await client.request("GET", url)
        finally:
            await client.stop()
    finally:
        listener.close()
        await listener.wait_closed()
