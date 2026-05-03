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
"""Tests for @post_construct and @pre_destroy lifecycle annotations."""

from pyfly.context.lifecycle import post_construct, pre_destroy


class TestPostConstruct:
    def test_marks_method(self):
        class MyService:
            @post_construct
            async def init(self):
                pass

        assert MyService.init.__pyfly_post_construct__ is True

    def test_preserves_method(self):
        class MyService:
            @post_construct
            async def init(self):
                self.ready = True

        svc = MyService()
        assert callable(svc.init)


class TestPreDestroy:
    def test_marks_method(self):
        class MyService:
            @pre_destroy
            async def cleanup(self):
                pass

        assert MyService.cleanup.__pyfly_pre_destroy__ is True

    def test_preserves_method(self):
        class MyService:
            @pre_destroy
            async def cleanup(self):
                self.closed = True

        svc = MyService()
        assert callable(svc.cleanup)


class TestMultipleAnnotations:
    def test_class_can_have_both(self):
        class MyService:
            @post_construct
            async def start(self):
                pass

            @pre_destroy
            async def stop(self):
                pass

        assert MyService.start.__pyfly_post_construct__ is True
        assert MyService.stop.__pyfly_pre_destroy__ is True
