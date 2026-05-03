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
"""Tests for admin configuration properties."""

from pyfly.admin.config import AdminProperties
from pyfly.core.config import Config


class TestAdminProperties:
    def test_defaults(self):
        props = AdminProperties()
        assert props.enabled is True
        assert props.path == "/admin"
        assert props.title == "PyFly Admin"
        assert props.theme == "auto"
        assert props.require_auth is False
        assert props.allowed_roles == ["ADMIN"]
        assert props.refresh_interval == 5000

    def test_from_config(self):
        config = Config({"pyfly": {"admin": {"title": "My Admin", "path": "/dashboard"}}})
        props = config.bind(AdminProperties)
        assert props.title == "My Admin"
        assert props.path == "/dashboard"

    def test_server_defaults(self):
        from pyfly.admin.config import AdminServerProperties

        props = AdminServerProperties()
        assert props.enabled is False
        assert props.poll_interval == 10000
