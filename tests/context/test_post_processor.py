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
"""Tests for BeanPostProcessor protocol."""

from pyfly.context.post_processor import BeanPostProcessor


class TestBeanPostProcessor:
    def test_protocol_is_structural(self):
        class MyProcessor:
            def before_init(self, bean, bean_name):
                return bean

            def after_init(self, bean, bean_name):
                return bean

        assert isinstance(MyProcessor(), BeanPostProcessor)

    def test_non_conforming_rejected(self):
        class NotAProcessor:
            pass

        assert not isinstance(NotAProcessor(), BeanPostProcessor)

    def test_processor_can_modify_bean(self):
        class WrapperProcessor:
            def before_init(self, bean, bean_name):
                return bean

            def after_init(self, bean, bean_name):
                bean._wrapped = True
                return bean

        class MyService:
            pass

        processor = WrapperProcessor()
        svc = MyService()
        result = processor.after_init(svc, "myService")
        assert result._wrapped is True
