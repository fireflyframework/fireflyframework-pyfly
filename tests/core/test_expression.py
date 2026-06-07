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
"""SpEL-lite expression evaluator (v26.06.30): @Value('#{...}') + @conditional_on_expression."""

from __future__ import annotations

import pytest

from pyfly.container.container import Container
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.context.conditions import conditional_on_expression
from pyfly.core.config import Config
from pyfly.core.expression import ExpressionError, evaluate, is_expression
from pyfly.core.value import Value


def test_is_expression() -> None:
    assert is_expression("#{1 + 1}")
    assert not is_expression("${key}")
    assert not is_expression("literal")


def test_arithmetic_comparison_boolean_ternary() -> None:
    assert evaluate("#{2 * 5 + 1}") == 11
    assert evaluate("#{10 % 3}") == 1
    assert evaluate("#{3 > 2 and 1 < 2}") is True
    assert evaluate("#{3 < 2 or 5 == 5}") is True
    assert evaluate("#{not false}") is True
    assert evaluate("#{100 if 2 > 1 else 200}") == 100
    assert evaluate("#{[1, 2, 3]}") == [1, 2, 3]


def test_placeholder_substitution() -> None:
    cfg = Config({"pyfly": {"workers": 4}})
    assert evaluate("#{${pyfly.workers} * 2}", cfg) == 8
    assert evaluate("#{${pyfly.missing:3} + 1}", cfg) == 4  # default used


def test_env_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYFLY_TEST_FLAG", "on")
    assert evaluate("#{env['PYFLY_TEST_FLAG'] == 'on'}") is True


def test_rejects_unsafe_constructs() -> None:
    with pytest.raises(ExpressionError):
        evaluate("#{__import__('os')}")  # function call — not whitelisted
    with pytest.raises(ExpressionError):
        evaluate("#{(1).__class__}")  # attribute access — not whitelisted
    with pytest.raises(ExpressionError):
        evaluate("#{unknown_name}")  # unknown name


def test_value_descriptor_handles_spel() -> None:
    cfg = Config({"pyfly": {"workers": 4}})
    assert Value("#{${pyfly.workers} * 2}").resolve(cfg) == 8
    assert Value("${pyfly.workers}").resolve(cfg) == 4  # plain placeholder still works
    assert Value("literal").resolve(cfg) == "literal"


def test_conditional_on_expression() -> None:
    evaluator = ConditionEvaluator(Config({"pyfly": {"workers": 4}}), Container())

    @conditional_on_expression("#{${pyfly.workers} > 1}")
    class Enabled:
        pass

    @conditional_on_expression("#{${pyfly.workers} > 10}")
    class Disabled:
        pass

    assert evaluator.should_include(Enabled) is True
    assert evaluator.should_include(Disabled) is False
