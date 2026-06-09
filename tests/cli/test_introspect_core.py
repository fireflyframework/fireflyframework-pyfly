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
"""Tests for the introspection core helpers."""

from __future__ import annotations

import pytest

from pyfly.cli._introspect import ActuatorClient, boot_context, run_async


def test_run_async_runs_coroutine() -> None:
    async def _c() -> int:
        return 42

    assert run_async(_c()) == 42


def test_boot_context_from_app_class() -> None:
    from pyfly.core.application import pyfly_application

    @pyfly_application(name="introspect-fixture")
    class App:
        pass

    ctx = boot_context(app_class=App)
    assert ctx is not None
    assert hasattr(ctx, "container")


class TestActuatorClient:
    def test_get_builds_url_and_parses_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        class _Resp:
            def raise_for_status(self) -> None: ...

            def json(self) -> dict:
                return {"status": "UP"}

        class _Client:
            def __init__(self, *a, **k) -> None: ...

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *a) -> None: ...

            def get(self, url: str):
                captured["url"] = url
                return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)
        client = ActuatorClient("http://host:8080")
        data = client.get("health")
        assert data == {"status": "UP"}
        assert captured["url"] == "http://host:8080/actuator/health"

    def test_trailing_slash_normalized(self) -> None:
        assert ActuatorClient("http://h:1/")._base == "http://h:1"
