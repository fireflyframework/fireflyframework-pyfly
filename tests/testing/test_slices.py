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
"""Tests for test slice decorators."""

from __future__ import annotations

from pyfly.testing.slices import DataTest, ServiceTest, WebTest, get_test_slice


class TestWebTestSlice:
    def test_marks_class_as_web_slice(self) -> None:
        @WebTest
        class MyTest:
            pass

        assert get_test_slice(MyTest) == "web"

    def test_preserves_class_identity(self) -> None:
        @WebTest
        class MyTest:
            pass

        assert MyTest.__name__ == "MyTest"


class TestDataTestSlice:
    def test_marks_class_as_data_slice(self) -> None:
        @DataTest
        class MyTest:
            pass

        assert get_test_slice(MyTest) == "data"


class TestServiceTestSlice:
    def test_marks_class_as_service_slice(self) -> None:
        @ServiceTest
        class MyTest:
            pass

        assert get_test_slice(MyTest) == "service"


class TestGetTestSlice:
    def test_returns_none_for_unsliced_class(self) -> None:
        class PlainTest:
            pass

        assert get_test_slice(PlainTest) is None
