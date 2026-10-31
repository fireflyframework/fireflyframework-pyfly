# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Shared fail-closed native listener client-authentication policy."""

from __future__ import annotations

import ssl
from typing import Any


def client_verify_mode(config: Any) -> ssl.VerifyMode:
    mode = getattr(config, "ssl_cert_reqs", "none")
    if mode not in {"none", "required"}:
        raise ValueError("pyfly.server.ssl-cert-reqs must be none or required")
    ca = getattr(config, "ssl_ca_certs", None)
    if mode == "required" and not (ca and config.ssl_certfile and config.ssl_keyfile):
        raise ValueError("Required TLS client authentication needs a client CA and server certificate/key")
    if ca and mode != "required":
        raise ValueError("A TLS client CA requires explicit required client authentication")
    return ssl.CERT_REQUIRED if mode == "required" else ssl.CERT_NONE
