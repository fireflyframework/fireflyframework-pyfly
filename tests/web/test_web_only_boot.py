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
"""Regression: ``create_app()`` must boot without the security extra (pyjwt).

A ``pyfly[web]``-only install has no ``pyjwt``. The web adapters must NOT
hard-import ``pyfly.security.oauth2.login`` (which imports ``jwt`` at module
load) while collecting routes — otherwise ``create_app()`` crashes at build
time. Both the Starlette and FastAPI adapters guard that import.
"""

from __future__ import annotations

import sys

import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


def _simulate_missing_security(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``from pyfly.security.oauth2.login import ...`` to raise ImportError,
    exactly as a ``pyfly[web]``-only install (no pyjwt) would at runtime."""
    monkeypatch.setitem(sys.modules, "pyfly.security.oauth2.login", None)


async def _ping(request: object) -> JSONResponse:
    return JSONResponse({"ok": True})


def test_starlette_create_app_boots_without_security_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.web.adapters.starlette import create_app

    _simulate_missing_security(monkeypatch)
    ctx = ApplicationContext(Config({}))
    app = create_app(title="web-only", version="1.0.0", context=ctx, extra_routes=[Route("/ping", _ping)])

    client = TestClient(app)
    assert client.get("/ping").json() == {"ok": True}


def test_fastapi_create_app_boots_without_security_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    from pyfly.web.adapters.fastapi.app import create_app as fastapi_create_app

    _simulate_missing_security(monkeypatch)
    ctx = ApplicationContext(Config({}))
    app = fastapi_create_app(title="web-only", version="1.0.0", context=ctx, extra_routes=[Route("/ping", _ping)])

    client = TestClient(app)
    assert client.get("/ping").json() == {"ok": True}
