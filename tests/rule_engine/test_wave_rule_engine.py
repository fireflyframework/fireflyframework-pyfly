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
"""Regression tests for rule-engine fixes (#215, #216, #222)."""

from __future__ import annotations

import pytest

from pyfly.rule_engine.dsl import Action, Condition, Rule
from pyfly.rule_engine.evaluator import RuleEvaluator


def test_unknown_action_type_recorded_not_silent() -> None:
    rule = Rule(id="r1", then=[Action(type="calculate", target="x")])
    result = RuleEvaluator().evaluate(rule, {})
    # The unsupported action is isolated and surfaced, not silently ignored
    # (audit #215 + #216).
    assert result.error is not None
    assert "calculate" in result.error
    assert result.actions_executed == []


def test_action_isolation_continues_after_failure() -> None:
    rule = Rule(
        id="r2",
        then=[
            Action(type="calculate", target="bad"),  # raises
            Action(type="set", target="ok", value=1),  # still runs
        ],
    )
    ctx: dict = {}
    result = RuleEvaluator().evaluate(rule, ctx)
    assert ctx.get("ok") == 1  # audit #216 — second action ran despite the first failing
    assert result.error is not None


def test_not_requires_single_child() -> None:
    bad = Condition(operator="not", children=[Condition(operator="eq", field="a", value=1), Condition(operator="eq")])
    rule = Rule(id="r3", when=bad, then=[])
    result = RuleEvaluator().evaluate(rule, {"a": 1})
    # A malformed 'not' surfaces as an error rather than silently using only
    # the first child (audit #222).
    assert result.matched is False
    assert result.error is not None
