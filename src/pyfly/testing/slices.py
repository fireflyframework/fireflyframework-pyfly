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
"""Test slice decorators for focused testing of specific layers."""

from __future__ import annotations

_SLICE_MARKER = "__pyfly_test_slice__"


def WebTest(cls: type) -> type:  # noqa: N802
    """Mark test class as a web-layer test slice.

    Loads web controllers and filters; mocks service layer.
    """
    setattr(cls, _SLICE_MARKER, "web")
    return cls


def DataTest(cls: type) -> type:  # noqa: N802
    """Mark test class as a data-layer test slice.

    Loads repositories with in-memory backends; mocks service layer.
    """
    setattr(cls, _SLICE_MARKER, "data")
    return cls


def ServiceTest(cls: type) -> type:  # noqa: N802
    """Mark test class as a service-layer test slice.

    Loads services; mocks repositories.
    """
    setattr(cls, _SLICE_MARKER, "service")
    return cls


def get_test_slice(cls: type) -> str | None:
    """Get the test slice type for a class, or None if not sliced."""
    return getattr(cls, _SLICE_MARKER, None)
