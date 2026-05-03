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
"""Tests for package scanning and auto-discovery."""

from pyfly.container import Container, service
from pyfly.container.scanner import scan_package
from pyfly.container.stereotypes import component, repository


@service
class ScannableServiceA:
    pass


@service
class ScannableServiceB:
    pass


@component
class ScannableComponent:
    pass


@repository
class ScannableRepository:
    pass


class TestPackageScanner:
    def test_scan_finds_stereotype_decorated_classes(self):
        container = Container()
        # Scan this module which has stereotype-decorated classes
        found = scan_package("tests.container.test_scanner", container)
        assert found >= 4  # ScannableServiceA, ScannableServiceB, ScannableComponent, ScannableRepository
