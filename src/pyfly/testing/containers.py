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
"""Test container factory for integration testing."""

from __future__ import annotations

from pyfly.container import Container, Scope


def create_test_container(
    overrides: dict[type, type] | None = None,
) -> Container:
    """Create a pre-configured Container for testing.

    Registers any override mappings (interface -> test implementation)
    so tests can substitute real services with fakes/mocks.

    Args:
        overrides: Mapping of interface types to test implementations.

    Returns:
        A configured Container ready for testing.
    """
    container = Container()

    if overrides:
        for interface, impl in overrides.items():
            container.register(impl, scope=Scope.SINGLETON)
            if interface != impl:
                container.bind(interface, impl)

    return container
