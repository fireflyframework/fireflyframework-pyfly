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
"""Wiring tests for the SP-1 integration foundation (do NOT require Docker)."""
from __future__ import annotations

import pytest


def test_unavailable_helper_skips_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.integration.conftest as it

    monkeypatch.setattr(it, "REQUIRE_DOCKER", False)
    with pytest.raises(pytest.skip.Exception):
        it.unavailable("no backend")


def test_unavailable_helper_fails_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.integration.conftest as it

    monkeypatch.setattr(it, "REQUIRE_DOCKER", True)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        it.unavailable("no backend")
    assert "no backend" in str(excinfo.value)


@pytest.mark.parametrize("name", ["redis_url", "pg_url", "mysql_url", "mongo_url", "kafka_url", "amqp_url"])
def test_backend_fixture_is_registered(request: pytest.FixtureRequest, name: str) -> None:
    assert name in request.fixturenames or request._fixturemanager.getfixturedefs(name, request.node) is not None
