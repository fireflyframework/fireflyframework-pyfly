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
"""Unit tests for ConditionEvaluator."""

from pyfly.container.container import Container
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.context.conditions import (
    conditional_on_bean,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config


def _make_evaluator(data: dict | None = None) -> tuple[ConditionEvaluator, Container]:
    config = Config(data or {})
    container = Container()
    return ConditionEvaluator(config, container), container


# ------------------------------------------------------------------
# on_property
# ------------------------------------------------------------------


class TestOnProperty:
    def test_matching_value_passes(self):
        evaluator, _ = _make_evaluator({"cache": {"enabled": "true"}})

        @conditional_on_property("cache.enabled", having_value="true")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is True

    def test_non_matching_value_fails(self):
        evaluator, _ = _make_evaluator({"cache": {"enabled": "false"}})

        @conditional_on_property("cache.enabled", having_value="true")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is False

    def test_missing_key_fails(self):
        evaluator, _ = _make_evaluator({})

        @conditional_on_property("cache.enabled", having_value="true")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is False

    def test_key_exists_no_value_constraint(self):
        evaluator, _ = _make_evaluator({"feature": {"flag": "anything"}})

        @conditional_on_property("feature.flag")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is True

    def test_key_missing_no_value_constraint(self):
        evaluator, _ = _make_evaluator({})

        @conditional_on_property("feature.flag")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is False


# ------------------------------------------------------------------
# on_class
# ------------------------------------------------------------------


class TestOnClass:
    def test_available_module_passes(self):
        evaluator, _ = _make_evaluator()

        @conditional_on_class("json")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is True

    def test_unavailable_module_fails(self):
        evaluator, _ = _make_evaluator()

        @conditional_on_class("nonexistent_xyz_module_12345")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is False


# ------------------------------------------------------------------
# on_missing_bean
# ------------------------------------------------------------------


class TestOnMissingBean:
    def test_passes_when_no_bean_registered(self):
        evaluator, _ = _make_evaluator()

        class CacheAdapter:
            pass

        @conditional_on_missing_bean(CacheAdapter)
        class FallbackCache:
            pass

        assert evaluator.should_include(FallbackCache, bean_pass=True) is True

    def test_fails_when_bean_registered(self):
        evaluator, container = _make_evaluator()

        class CacheAdapter:
            pass

        class RedisCache(CacheAdapter):
            pass

        container.register(RedisCache)

        @conditional_on_missing_bean(CacheAdapter)
        class FallbackCache:
            pass

        assert evaluator.should_include(FallbackCache, bean_pass=True) is False

    def test_fails_when_exact_type_bean_registered(self):
        # Regression: a bean registered as EXACTLY the type must make
        # @conditional_on_missing_bean back off (previously the exact match was
        # skipped, so the default ALSO registered -> two beans of the same type).
        evaluator, container = _make_evaluator()

        class CacheAdapter:
            pass

        container.register(CacheAdapter)  # exact type, not a subclass

        @conditional_on_missing_bean(CacheAdapter)
        class FallbackCache:
            pass

        assert evaluator.should_include(FallbackCache, bean_pass=True) is False


# ------------------------------------------------------------------
# on_bean
# ------------------------------------------------------------------


class TestOnBean:
    def test_passes_when_bean_registered(self):
        evaluator, container = _make_evaluator()

        class DataSource:
            pass

        class PostgresDataSource(DataSource):
            pass

        container.register(PostgresDataSource)

        @conditional_on_bean(DataSource)
        class TransactionManager:
            pass

        assert evaluator.should_include(TransactionManager, bean_pass=True) is True

    def test_fails_when_no_bean_registered(self):
        evaluator, _ = _make_evaluator()

        class DataSource:
            pass

        @conditional_on_bean(DataSource)
        class TransactionManager:
            pass

        assert evaluator.should_include(TransactionManager, bean_pass=True) is False

    def test_passes_when_exact_type_bean_registered(self):
        # Regression: a bean registered as EXACTLY the type must satisfy
        # @conditional_on_bean(T) (previously the exact match was skipped, so the
        # condition wrongly failed unless a *subclass* of T was registered).
        evaluator, container = _make_evaluator()

        class DataSource:
            pass

        container.register(DataSource)  # exact type, not a subclass

        @conditional_on_bean(DataSource)
        class TransactionManager:
            pass

        assert evaluator.should_include(TransactionManager, bean_pass=True) is True


# ------------------------------------------------------------------
# Stacked conditions (AND semantics)
# ------------------------------------------------------------------


class TestStackedConditions:
    def test_all_pass(self):
        evaluator, _ = _make_evaluator({"cache": {"enabled": "true"}})

        @conditional_on_class("json")
        @conditional_on_property("cache.enabled", having_value="true")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is True

    def test_one_fails(self):
        evaluator, _ = _make_evaluator({"cache": {"enabled": "false"}})

        @conditional_on_class("json")
        @conditional_on_property("cache.enabled", having_value="true")
        class Bean:
            pass

        assert evaluator.should_include(Bean, bean_pass=False) is False


# ------------------------------------------------------------------
# __pyfly_condition__ (singular callable from stereotype)
# ------------------------------------------------------------------


class TestSingularCondition:
    def test_callable_returning_true(self):
        evaluator, _ = _make_evaluator()

        class Bean:
            __pyfly_condition__ = staticmethod(lambda: True)

        assert evaluator.should_include(Bean, bean_pass=False) is True

    def test_callable_returning_false(self):
        evaluator, _ = _make_evaluator()

        class Bean:
            __pyfly_condition__ = staticmethod(lambda: False)

        assert evaluator.should_include(Bean, bean_pass=False) is False

    def test_none_condition_passes(self):
        evaluator, _ = _make_evaluator()

        class Bean:
            __pyfly_condition__ = None

        assert evaluator.should_include(Bean, bean_pass=False) is True


# ------------------------------------------------------------------
# No conditions at all
# ------------------------------------------------------------------


class TestNoConditions:
    def test_plain_class_always_included(self):
        evaluator, _ = _make_evaluator()

        class PlainBean:
            pass

        assert evaluator.should_include(PlainBean, bean_pass=False) is True
        assert evaluator.should_include(PlainBean, bean_pass=True) is True
