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
"""Tests for @projection decorator and utilities."""

from typing import Protocol

from pyfly.data.projection import is_projection, projection, projection_fields


@projection
class OrderSummary(Protocol):
    id: str
    status: str
    total: float


class NotAProjection(Protocol):
    name: str


class TestProjectionDecorator:
    def test_marks_class_as_projection(self) -> None:
        assert is_projection(OrderSummary) is True

    def test_unmarked_class_is_not_projection(self) -> None:
        assert is_projection(NotAProjection) is False

    def test_plain_class_is_not_projection(self) -> None:
        assert is_projection(str) is False


class TestProjectionFields:
    def test_extracts_field_names(self) -> None:
        fields = projection_fields(OrderSummary)
        assert fields == ["id", "status", "total"]

    def test_empty_for_no_hints(self) -> None:
        @projection
        class Empty(Protocol):
            pass

        assert projection_fields(Empty) == []
