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
"""Spring Boot 2.4+ profile expression grammar (v26.06.39): &, |, !, () grouping."""

from __future__ import annotations

import pytest

from pyfly.context.environment import Environment
from pyfly.core.config import Config


def _env(active: str, monkeypatch: pytest.MonkeyPatch) -> Environment:
    monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", active)
    env = Environment(Config({}))
    return env


def test_and(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("prod,cloud", monkeypatch)
    assert env.active_profiles == ["prod", "cloud"]
    assert env.accepts_profiles("prod & cloud") is True
    assert env.accepts_profiles("prod & staging") is False


def test_or(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("prod", monkeypatch)
    assert env.accepts_profiles("prod | qa") is True
    assert env.accepts_profiles("dev | qa") is False


def test_not(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("prod", monkeypatch)
    assert env.accepts_profiles("!test") is True
    assert env.accepts_profiles("!prod") is False
    assert env.accepts_profiles("prod & !test") is True


def test_grouping(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("cloud,qa", monkeypatch)
    assert env.accepts_profiles("(prod & cloud) | qa") is True  # qa branch
    assert env.accepts_profiles("(prod & cloud) & qa") is False  # prod not active
    assert env.accepts_profiles("!(prod | dev)") is True  # neither active


def test_legacy_comma_and_simple_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("dev", monkeypatch)
    assert env.accepts_profiles("dev,test") is True  # legacy comma-OR
    assert env.accepts_profiles("dev") is True
    assert env.accepts_profiles("test") is False
