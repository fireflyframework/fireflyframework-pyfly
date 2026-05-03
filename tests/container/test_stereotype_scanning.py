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
"""Tests for scanning stereotype-decorated classes."""

from pyfly.container.container import Container
from pyfly.container.scanner import scan_module_classes
from pyfly.container.stereotypes import component, repository, service


@service
class ScanTestService:
    pass


@repository
class ScanTestRepo:
    pass


@component
class ScanTestComponent:
    pass


class NotABean:
    pass


class TestStereotypeScanning:
    def test_scan_finds_all_stereotypes(self):
        import tests.container.test_stereotype_scanning as mod

        classes = scan_module_classes(mod)
        names = {cls.__name__ for cls in classes}
        assert "ScanTestService" in names
        assert "ScanTestRepo" in names
        assert "ScanTestComponent" in names
        assert "NotABean" not in names

    def test_scan_and_register(self):
        import tests.container.test_stereotype_scanning as mod

        c = Container()
        classes = scan_module_classes(mod)
        for cls in classes:
            c.register(cls)
        svc = c.resolve(ScanTestService)
        assert isinstance(svc, ScanTestService)
