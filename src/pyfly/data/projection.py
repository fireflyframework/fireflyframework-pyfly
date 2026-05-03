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
"""Projection marker and utilities for interface-based projections."""

from __future__ import annotations

from typing import get_type_hints

_PROJECTION_MARKER = "__pyfly_projection__"


def projection(cls: type) -> type:
    """Mark a Protocol class as a projection interface.

    Projections declare a subset of entity fields. Query compilers
    use them to select only the required columns/fields.

    Usage::

        @projection
        class OrderSummary(Protocol):
            id: str
            status: str
            total: float
    """
    setattr(cls, _PROJECTION_MARKER, True)
    return cls


def is_projection(cls: type) -> bool:
    """Check if a type is marked as a projection."""
    return getattr(cls, _PROJECTION_MARKER, False) is True


def projection_fields(cls: type) -> list[str]:
    """Get the field names declared on a projection type."""
    hints = get_type_hints(cls)
    return [name for name in hints if not name.startswith("_")]
