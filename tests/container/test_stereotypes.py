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
"""Tests for Spring-style stereotype decorators."""

from pyfly.container.stereotypes import (
    component,
    configuration,
    controller,
    repository,
    rest_controller,
    service,
)
from pyfly.container.types import Scope


class TestStereotypeDecorators:
    def test_component_marks_class(self):
        @component
        class MyComponent:
            pass

        assert MyComponent.__pyfly_injectable__ is True
        assert MyComponent.__pyfly_stereotype__ == "component"

    def test_service_marks_class(self):
        @service
        class MyService:
            pass

        assert MyService.__pyfly_injectable__ is True
        assert MyService.__pyfly_stereotype__ == "service"

    def test_repository_marks_class(self):
        @repository
        class MyRepo:
            pass

        assert MyRepo.__pyfly_injectable__ is True
        assert MyRepo.__pyfly_stereotype__ == "repository"

    def test_controller_marks_class(self):
        @controller
        class MyCtrl:
            pass

        assert MyCtrl.__pyfly_injectable__ is True
        assert MyCtrl.__pyfly_stereotype__ == "controller"

    def test_rest_controller_marks_class(self):
        @rest_controller
        class MyRest:
            pass

        assert MyRest.__pyfly_injectable__ is True
        assert MyRest.__pyfly_stereotype__ == "rest_controller"

    def test_configuration_marks_class(self):
        @configuration
        class MyConfig:
            pass

        assert MyConfig.__pyfly_injectable__ is True
        assert MyConfig.__pyfly_stereotype__ == "configuration"


class TestStereotypeWithArguments:
    def test_service_with_scope(self):
        @service(scope=Scope.TRANSIENT)
        class MyService:
            pass

        assert MyService.__pyfly_scope__ == Scope.TRANSIENT
        assert MyService.__pyfly_stereotype__ == "service"

    def test_component_with_name(self):
        @component(name="myBean")
        class MyComponent:
            pass

        assert MyComponent.__pyfly_bean_name__ == "myBean"

    def test_service_with_profile(self):
        @service(profile="production")
        class ProdService:
            pass

        assert ProdService.__pyfly_profile__ == "production"

    def test_rest_controller_with_all_args(self):
        @rest_controller(name="orderCtrl", scope=Scope.SINGLETON, profile="api")
        class OrderCtrl:
            pass

        assert OrderCtrl.__pyfly_bean_name__ == "orderCtrl"
        assert OrderCtrl.__pyfly_scope__ == Scope.SINGLETON
        assert OrderCtrl.__pyfly_profile__ == "api"
        assert OrderCtrl.__pyfly_stereotype__ == "rest_controller"

    def test_component_with_condition(self):
        def my_condition():
            return True

        @component(condition=my_condition)
        class ConditionalComponent:
            pass

        assert ConditionalComponent.__pyfly_condition__ is my_condition

    def test_stereotype_preserves_class_identity(self):
        @service
        class MyService:
            """My docstring."""

            def method(self):
                return 42

        assert MyService.__name__ == "MyService"
        assert MyService.__doc__ == "My docstring."
        assert MyService().method() == 42
