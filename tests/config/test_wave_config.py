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
"""Regression tests for config placeholder env resolution (#87, #89)."""

from __future__ import annotations

from pyfly.core.config import Config


def test_placeholder_honors_relaxed_env_override(monkeypatch) -> None:
    # ${app.name} must resolve via the PYFLY_APP_NAME relaxed env mapping, and
    # the env override must win over the raw file value (audit #87/#89).
    monkeypatch.setenv("PYFLY_APP_NAME", "from-env")
    cfg = Config({"app": {"name": "from-file"}, "greeting": "Hi ${app.name}"})
    assert cfg.get("greeting") == "Hi from-env"


def test_placeholder_falls_back_to_file_when_no_env() -> None:
    cfg = Config({"app": {"name": "from-file"}, "greeting": "Hi ${app.name}"})
    assert cfg.get("greeting") == "Hi from-file"
