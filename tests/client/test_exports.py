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
"""Tests for client module public exports."""

from __future__ import annotations


class TestClientExports:
    def test_can_import_declarative(self) -> None:
        from pyfly.client import http_client

        assert http_client is not None

    def test_can_import_post_processor(self) -> None:
        from pyfly.client import HttpClientBeanPostProcessor

        assert HttpClientBeanPostProcessor is not None

    def test_can_import_service_client(self) -> None:
        from pyfly.client import service_client

        assert service_client is not None
