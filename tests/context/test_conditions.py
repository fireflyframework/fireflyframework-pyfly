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
"""Tests for conditional bean decorators."""

from pyfly.container.types import Scope
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_bean,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)


class TestConditionalOnProperty:
    def test_marks_class(self):
        @conditional_on_property("cache.enabled", having_value="true")
        class MyBean:
            pass

        cond = MyBean.__pyfly_conditions__
        assert len(cond) == 1
        assert cond[0]["type"] == "on_property"
        assert cond[0]["key"] == "cache.enabled"
        assert cond[0]["having_value"] == "true"

    def test_stacks_with_other_conditions(self):
        @conditional_on_property("a", having_value="1")
        @conditional_on_property("b", having_value="2")
        class MyBean:
            pass

        cond = MyBean.__pyfly_conditions__
        assert len(cond) == 2


class TestConditionalOnClass:
    def test_marks_class(self):
        @conditional_on_class("json")
        class MyBean:
            pass

        cond = MyBean.__pyfly_conditions__
        assert len(cond) == 1
        assert cond[0]["type"] == "on_class"
        assert cond[0]["module_name"] == "json"

    def test_evaluates_available(self):
        @conditional_on_class("json")
        class MyBean:
            pass

        cond = MyBean.__pyfly_conditions__[0]
        assert cond["check"]() is True

    def test_evaluates_unavailable(self):
        @conditional_on_class("nonexistent_xyz_module_12345")
        class MyBean:
            pass

        cond = MyBean.__pyfly_conditions__[0]
        assert cond["check"]() is False


class TestConditionalOnMissingBean:
    def test_marks_class(self):
        class SomeInterface:
            pass

        @conditional_on_missing_bean(SomeInterface)
        class FallbackImpl:
            pass

        cond = FallbackImpl.__pyfly_conditions__
        assert len(cond) == 1
        assert cond[0]["type"] == "on_missing_bean"
        assert cond[0]["bean_type"] is SomeInterface


class TestConditionalOnBean:
    def test_marks_class(self):
        class DataSource:
            pass

        @conditional_on_bean(DataSource)
        class TransactionManager:
            pass

        cond = TransactionManager.__pyfly_conditions__
        assert len(cond) == 1
        assert cond[0]["type"] == "on_bean"
        assert cond[0]["bean_type"] is DataSource

    def test_stacks_with_other_conditions(self):
        class Foo:
            pass

        @conditional_on_bean(Foo)
        @conditional_on_property("x.y")
        class MyBean:
            pass

        cond = MyBean.__pyfly_conditions__
        assert len(cond) == 2
        assert cond[0]["type"] == "on_property"
        assert cond[1]["type"] == "on_bean"


class TestAutoConfiguration:
    def test_sets_stereotype_and_flags(self):
        @auto_configuration
        class MyAutoConfig:
            pass

        assert MyAutoConfig.__pyfly_auto_configuration__ is True
        assert MyAutoConfig.__pyfly_injectable__ is True
        assert MyAutoConfig.__pyfly_stereotype__ == "configuration"
        assert MyAutoConfig.__pyfly_scope__ == Scope.SINGLETON
        assert MyAutoConfig.__pyfly_order__ == 1000

    def test_preserves_existing_scope(self):
        class PreScoped:
            __pyfly_scope__ = Scope.TRANSIENT

        auto_configuration(PreScoped)
        assert PreScoped.__pyfly_scope__ == Scope.TRANSIENT

    def test_preserves_existing_order(self):
        class PreOrdered:
            __pyfly_order__ = 42

        auto_configuration(PreOrdered)
        assert PreOrdered.__pyfly_order__ == 42
