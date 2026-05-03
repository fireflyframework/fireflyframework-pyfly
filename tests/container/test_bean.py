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
"""Tests for @bean factory methods, @primary, and Qualifier."""

from typing import Annotated

from pyfly.container.bean import Qualifier, bean, primary
from pyfly.container.stereotypes import configuration
from pyfly.container.types import Scope


class TestBeanDecorator:
    def test_bean_marks_method(self):
        @configuration
        class MyConfig:
            @bean
            def my_service(self) -> str:
                return "hello"

        assert MyConfig.my_service.__pyfly_bean__ is True

    def test_bean_with_name(self):
        @configuration
        class MyConfig:
            @bean(name="customName")
            def my_service(self) -> str:
                return "hello"

        assert MyConfig.my_service.__pyfly_bean_name__ == "customName"

    def test_bean_with_scope(self):
        @configuration
        class MyConfig:
            @bean(scope=Scope.TRANSIENT)
            def my_service(self) -> str:
                return "hello"

        assert MyConfig.my_service.__pyfly_bean_scope__ == Scope.TRANSIENT

    def test_bean_default_scope_is_singleton(self):
        @configuration
        class MyConfig:
            @bean
            def my_service(self) -> str:
                return "hello"

        assert MyConfig.my_service.__pyfly_bean_scope__ == Scope.SINGLETON


class TestPrimary:
    def test_primary_marks_class(self):
        @primary
        class MyImpl:
            pass

        assert MyImpl.__pyfly_primary__ is True

    def test_primary_preserves_class(self):
        @primary
        class MyImpl:
            def method(self):
                return 42

        assert MyImpl().method() == 42


class TestQualifier:
    def test_qualifier_stores_name(self):
        q = Qualifier("myBean")
        assert q.name == "myBean"

    def test_qualifier_repr(self):
        q = Qualifier("myBean")
        assert "myBean" in repr(q)

    def test_qualifier_in_annotated(self):
        hint = Annotated[str, Qualifier("primary_db")]
        metadata = hint.__metadata__
        assert len(metadata) == 1
        assert isinstance(metadata[0], Qualifier)
        assert metadata[0].name == "primary_db"
