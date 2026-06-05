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
"""Regression: the error envelope renders even when an exception's context holds
non-JSON-serializable values (v26.06.14).

A Pydantic ``field_validator`` that raises ``ValueError`` (which is how the
framework's own ``valid_iban`` / ``valid_currency_code`` markers work) captures
the raw ``ValueError`` into ``ctx``. Previously the global handler dumped the
context verbatim and ``json.dumps`` crashed, returning a bare 500 with an empty
body instead of the promised structured 422.
"""

from __future__ import annotations

from typing import Any

from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.kernel.exceptions import ValidationException
from pyfly.web.adapters.starlette import create_app


async def _validation_with_unserializable_ctx(request: Any) -> Any:
    raise ValidationException(
        "Validation failed: iban: invalid IBAN",
        code="VALIDATION_ERROR",
        context={
            "errors": [{"loc": ["body", "iban"], "msg": "invalid IBAN", "ctx": {"error": ValueError("invalid IBAN")}}]
        },
    )


def test_unserializable_context_renders_422_not_500() -> None:
    app = create_app(title="t", extra_routes=[Route("/v", _validation_with_unserializable_ctx)])
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/v")

    assert resp.status_code == 422  # not a bare 500
    body = resp.json()  # must be valid JSON — would crash before the fix
    assert body["error"]["code"] == "VALIDATION_ERROR"
    err = body["error"]["context"]["errors"][0]
    assert err["loc"] == ["body", "iban"]
    assert err["ctx"]["error"] == "invalid IBAN"  # ValueError stringified, not dropped
