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
"""Tests for @order decorator and ordering constants."""

from pyfly.container.ordering import HIGHEST_PRECEDENCE, LOWEST_PRECEDENCE, order


class TestOrderDecorator:
    def test_sets_order_attribute(self):
        @order(5)
        class MyService:
            pass

        assert MyService.__pyfly_order__ == 5

    def test_preserves_class(self):
        @order(1)
        class MyService:
            """My doc."""

        assert MyService.__name__ == "MyService"
        assert MyService.__doc__ == "My doc."

    def test_negative_order(self):
        @order(-10)
        class EarlyService:
            pass

        assert EarlyService.__pyfly_order__ == -10

    def test_zero_order(self):
        @order(0)
        class DefaultService:
            pass

        assert DefaultService.__pyfly_order__ == 0

    def test_stacks_with_stereotype(self):
        from pyfly.container.stereotypes import service

        @order(3)
        @service
        class OrderedService:
            pass

        assert OrderedService.__pyfly_order__ == 3
        assert OrderedService.__pyfly_stereotype__ == "service"


class TestOrderConstants:
    def test_highest_precedence(self):
        assert HIGHEST_PRECEDENCE == -(2**31)

    def test_lowest_precedence(self):
        assert LOWEST_PRECEDENCE == 2**31 - 1

    def test_highest_less_than_lowest(self):
        assert HIGHEST_PRECEDENCE < LOWEST_PRECEDENCE
