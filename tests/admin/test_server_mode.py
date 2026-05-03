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
"""Tests for admin server mode — instance registry and discovery."""

from pyfly.admin.server.instance_registry import InstanceRegistry


class TestInstanceRegistry:
    def test_register_instance(self):
        registry = InstanceRegistry()
        registry.register("test-app", "http://localhost:8080")
        instances = registry.get_instances()
        assert len(instances) == 1
        assert instances[0].name == "test-app"
        assert instances[0].status == "UNKNOWN"

    def test_deregister_instance(self):
        registry = InstanceRegistry()
        registry.register("test-app", "http://localhost:8080")
        registry.deregister("test-app")
        assert len(registry.get_instances()) == 0

    def test_update_status(self):
        registry = InstanceRegistry()
        registry.register("test-app", "http://localhost:8080")
        registry.update_status("test-app", "UP")
        inst = registry.get_instance("test-app")
        assert inst.status == "UP"
        assert inst.last_checked is not None

    def test_get_instance_not_found(self):
        registry = InstanceRegistry()
        assert registry.get_instance("nope") is None

    def test_to_dict(self):
        registry = InstanceRegistry()
        registry.register("app1", "http://localhost:8080")
        data = registry.to_dict()
        assert "instances" in data
        assert len(data["instances"]) == 1


class TestStaticDiscovery:
    def test_discover_registers_instances(self):
        from pyfly.admin.server.discovery import StaticDiscovery

        registry = InstanceRegistry()
        config = [
            {"name": "app-1", "url": "http://localhost:8080"},
            {"name": "app-2", "url": "http://localhost:8081"},
        ]
        discovery = StaticDiscovery(config, registry)
        discovery.discover()
        assert len(registry.get_instances()) == 2
        assert registry.get_instance("app-1") is not None
        assert registry.get_instance("app-2") is not None

    def test_discover_skips_incomplete_entries(self):
        from pyfly.admin.server.discovery import StaticDiscovery

        registry = InstanceRegistry()
        config = [
            {"name": "valid", "url": "http://localhost:8080"},
            {"name": "", "url": "http://localhost:8081"},
            {"url": "http://localhost:8082"},
            {"name": "no-url"},
        ]
        discovery = StaticDiscovery(config, registry)
        discovery.discover()
        assert len(registry.get_instances()) == 1
        assert registry.get_instance("valid") is not None
