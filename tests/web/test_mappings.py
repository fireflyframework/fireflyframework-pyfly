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
"""Tests for HTTP method mapping decorators."""

from pyfly.web.mappings import (
    delete_mapping,
    get_mapping,
    patch_mapping,
    post_mapping,
    put_mapping,
    request_mapping,
)


class TestRequestMapping:
    def test_class_level_path(self):
        @request_mapping("/api/orders")
        class MyController:
            pass

        assert MyController.__pyfly_request_mapping__ == "/api/orders"  # type: ignore[attr-defined]

    def test_preserves_class(self):
        @request_mapping("/api")
        class MyController:
            """My docstring."""

        assert MyController.__name__ == "MyController"
        assert MyController.__doc__ == "My docstring."


class TestGetMapping:
    def test_marks_method(self):
        class Ctrl:
            @get_mapping("/{item_id}")
            async def get_item(self):
                pass

        meta = Ctrl.get_item.__pyfly_mapping__
        assert meta["method"] == "GET"
        assert meta["path"] == "/{item_id}"
        assert meta["status_code"] == 200

    def test_preserves_method(self):
        class Ctrl:
            @get_mapping("/")
            async def list_items(self):
                return []

        assert callable(Ctrl.list_items)


class TestPostMapping:
    def test_marks_method(self):
        class Ctrl:
            @post_mapping("/", status_code=201)
            async def create(self):
                pass

        meta = Ctrl.create.__pyfly_mapping__
        assert meta["method"] == "POST"
        assert meta["status_code"] == 201


class TestPutMapping:
    def test_marks_method(self):
        class Ctrl:
            @put_mapping("/{id}")
            async def update(self):
                pass

        meta = Ctrl.update.__pyfly_mapping__
        assert meta["method"] == "PUT"
        assert meta["path"] == "/{id}"


class TestPatchMapping:
    def test_marks_method(self):
        class Ctrl:
            @patch_mapping("/{id}")
            async def partial_update(self):
                pass

        meta = Ctrl.partial_update.__pyfly_mapping__
        assert meta["method"] == "PATCH"


class TestDeleteMapping:
    def test_marks_method(self):
        class Ctrl:
            @delete_mapping("/{id}", status_code=204)
            async def remove(self):
                pass

        meta = Ctrl.remove.__pyfly_mapping__
        assert meta["method"] == "DELETE"
        assert meta["status_code"] == 204


class TestDefaultStatusCodes:
    def test_get_default_200(self):
        class Ctrl:
            @get_mapping("/")
            async def get(self):
                pass

        assert Ctrl.get.__pyfly_mapping__["status_code"] == 200

    def test_post_default_200(self):
        class Ctrl:
            @post_mapping("/")
            async def create(self):
                pass

        assert Ctrl.create.__pyfly_mapping__["status_code"] == 200

    def test_delete_default_200(self):
        class Ctrl:
            @delete_mapping("/")
            async def remove(self):
                pass

        assert Ctrl.remove.__pyfly_mapping__["status_code"] == 200
