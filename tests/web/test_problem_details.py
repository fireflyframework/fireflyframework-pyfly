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
"""RFC 7807 problem+json error responses (v26.06.25) — opt-in via
``pyfly.web.problem-details.enabled`` (Spring Boot 3 parity)."""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from pyfly.kernel.exceptions import ResourceNotFoundException
from pyfly.web.adapters.starlette.errors import global_exception_handler


def _request(*, problem_details: bool) -> Request:
    class _State:
        pass

    state = _State()
    state.pyfly_problem_details = problem_details  # type: ignore[attr-defined]

    class _App:
        pass

    app = _App()
    app.state = state  # type: ignore[attr-defined]
    scope = {"type": "http", "method": "GET", "path": "/widgets/42", "headers": [], "app": app}
    return Request(scope)


@pytest.mark.asyncio
async def test_default_envelope_is_unchanged() -> None:
    resp = await global_exception_handler(
        _request(problem_details=False),
        ResourceNotFoundException("widget not found", code="WIDGET_NOT_FOUND"),
    )
    assert resp.media_type == "application/json"
    body = json.loads(bytes(resp.body))
    assert body["error"]["status"] == 404
    assert body["error"]["code"] == "WIDGET_NOT_FOUND"
    assert body["error"]["path"] == "/widgets/42"


@pytest.mark.asyncio
async def test_problem_details_when_enabled() -> None:
    resp = await global_exception_handler(
        _request(problem_details=True),
        ResourceNotFoundException("widget not found", code="WIDGET_NOT_FOUND"),
    )
    assert resp.media_type == "application/problem+json"
    body = json.loads(bytes(resp.body))
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert "widget not found" in body["detail"]
    assert body["instance"] == "/widgets/42"
    assert body["code"] == "WIDGET_NOT_FOUND"  # extension member
