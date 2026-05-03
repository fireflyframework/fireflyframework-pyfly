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
"""Tests for @exception_handler decorator."""

from pyfly.web.exception_handler import exception_handler


class OrderNotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


class TestExceptionHandler:
    def test_marks_method(self):
        class Ctrl:
            @exception_handler(OrderNotFoundError)
            async def handle_not_found(self, exc):
                return 404, {"error": "not found"}

        meta = Ctrl.handle_not_found.__pyfly_exception_handler__
        assert meta == OrderNotFoundError

    def test_preserves_method(self):
        class Ctrl:
            @exception_handler(OrderNotFoundError)
            async def handle_not_found(self, exc):
                return 404, {"error": "not found"}

        assert callable(Ctrl.handle_not_found)

    def test_multiple_handlers(self):
        class Ctrl:
            @exception_handler(OrderNotFoundError)
            async def handle_not_found(self, exc):
                return 404, {"error": "not found"}

            @exception_handler(ValidationError)
            async def handle_validation(self, exc):
                return 422, {"error": str(exc)}

        assert Ctrl.handle_not_found.__pyfly_exception_handler__ == OrderNotFoundError
        assert Ctrl.handle_validation.__pyfly_exception_handler__ == ValidationError
