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
"""Tests for FastAPI auto-configuration."""

from __future__ import annotations

from pyfly.web.auto_configuration import WebAutoConfiguration


class TestWebAutoConfiguration:
    def test_has_starlette_bean(self):
        assert hasattr(WebAutoConfiguration, "web_adapter")
        assert getattr(WebAutoConfiguration.web_adapter, "__pyfly_bean__", False)

    def test_has_fastapi_bean(self):
        assert hasattr(WebAutoConfiguration, "fastapi_adapter")
        assert getattr(WebAutoConfiguration.fastapi_adapter, "__pyfly_bean__", False)

    def test_fastapi_bean_has_conditions(self):
        conditions = getattr(WebAutoConfiguration.fastapi_adapter, "__pyfly_conditions__", [])
        condition_types = [c["type"] for c in conditions]
        assert "on_class" in condition_types
        assert "on_missing_bean" in condition_types

    def test_starlette_bean_has_conditions(self):
        conditions = getattr(WebAutoConfiguration.web_adapter, "__pyfly_conditions__", [])
        condition_types = [c["type"] for c in conditions]
        assert "on_class" in condition_types
        assert "on_missing_bean" in condition_types
