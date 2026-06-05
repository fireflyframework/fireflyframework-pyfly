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
"""Regression test: uvicorn serve_async honors full config (audit #226)."""

from __future__ import annotations

from types import SimpleNamespace

from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter


def test_build_kwargs_includes_ssl_and_tuning() -> None:
    config = SimpleNamespace(
        keep_alive_timeout=30,
        backlog=2048,
        graceful_timeout=10,
        ssl_certfile="/tls/cert.pem",
        ssl_keyfile="/tls/key.pem",
        max_concurrent_connections=500,
        max_requests_per_worker=10000,
    )
    kwargs = UvicornServerAdapter._build_kwargs("0.0.0.0", 8000, "auto", config)
    # The same SSL / keep-alive / backlog / graceful / limit settings the
    # blocking serve() applies must be available to serve_async() (audit #226).
    assert kwargs["ssl_certfile"] == "/tls/cert.pem"
    assert kwargs["ssl_keyfile"] == "/tls/key.pem"
    assert kwargs["timeout_keep_alive"] == 30
    assert kwargs["backlog"] == 2048
    assert kwargs["timeout_graceful_shutdown"] == 10
    assert kwargs["limit_concurrency"] == 500
    assert kwargs["limit_max_requests"] == 10000
    assert "workers" not in kwargs  # workers is run-level, not a Config kwarg
