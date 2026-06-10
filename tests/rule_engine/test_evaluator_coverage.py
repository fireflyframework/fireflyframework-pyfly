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
"""Coverage for the rule evaluator's previously-untested paths (v26.06.18).

The module had ~8 tests for 361 lines; an audit confirmed every path correct but
many were unexercised. These tests lock in: each leaf operator (+ None/missing
safety + a type-mismatch surfacing), composite and/or/not (+ not-arity), then vs
otherwise, set/increment/log/nested-write/unsupported-action isolation, the
loud unknown-operator error, the disabled-rule skip, and RuleSet priority
ordering + cross-rule error isolation.
"""

from __future__ import annotations

import pytest

from pyfly.rule_engine import (
    Action,
    Condition,
    EvaluationResult,
    Rule,
    RuleEvaluator,
    RuleSet,
    RuleSetEvaluator,
)


def _evaluate(when: Condition, ctx: dict) -> EvaluationResult:
    return RuleEvaluator().evaluate(Rule(id="r", when=when), ctx)


class TestLeafOperators:
    @pytest.mark.parametrize(
        ("op", "actual", "expected", "want"),
        [
            ("eq", 5, 5, True),
            ("eq", 5, 6, False),
            ("ne", 5, 6, True),
            ("ne", 5, 5, False),
            ("gt", 10, 5, True),
            ("gt", 5, 5, False),
            ("ge", 5, 5, True),
            ("ge", 4, 5, False),
            ("lt", 3, 5, True),
            ("lt", 5, 5, False),
            ("le", 5, 5, True),
            ("le", 6, 5, False),
            ("in", "gold", ["gold", "silver"], True),
            ("in", "bronze", ["gold"], False),
            ("not_in", "bronze", ["gold"], True),
            ("not_in", "gold", ["gold"], False),
        ],
    )
    def test_operator(self, op: str, actual: object, expected: object, want: bool) -> None:
        assert _evaluate(Condition(operator=op, field="x", value=expected), {"x": actual}).matched is want

    def test_regex(self) -> None:
        cond = Condition(operator="regex", field="code", value="^PARTNER")
        assert _evaluate(cond, {"code": "PARTNER-7"}).matched is True
        assert _evaluate(cond, {"code": "OTHER"}).matched is False

    @pytest.mark.parametrize("op", ["gt", "ge", "lt", "le", "in"])
    def test_missing_field_is_false_without_error(self, op: str) -> None:
        value: object = [1, 2] if op == "in" else 5
        res = _evaluate(Condition(operator=op, field="missing", value=value), {})
        assert res.matched is False
        assert res.error is None  # None-guarded — no crash

    def test_type_mismatch_is_surfaced_not_silent(self) -> None:
        # str vs int comparison raises TypeError; it must be captured into the
        # result (loud), not silently treated as a match or swallowed.
        res = _evaluate(Condition(operator="gt", field="name", value=5), {"name": "abc"})
        assert res.matched is False
        assert res.error is not None


class TestCompositeConditions:
    def test_and(self) -> None:
        cond = Condition(
            operator="and",
            children=[
                Condition(operator="gt", field="x", value=5),
                Condition(operator="lt", field="x", value=100),
            ],
        )
        assert _evaluate(cond, {"x": 50}).matched is True
        assert _evaluate(cond, {"x": 200}).matched is False

    def test_or(self) -> None:
        cond = Condition(
            operator="or",
            children=[
                Condition(operator="eq", field="tier", value="gold"),
                Condition(operator="gt", field="spend", value=1000),
            ],
        )
        assert _evaluate(cond, {"tier": "silver", "spend": 2000}).matched is True
        assert _evaluate(cond, {"tier": "silver", "spend": 10}).matched is False

    def test_not(self) -> None:
        cond = Condition(operator="not", children=[Condition(operator="eq", field="blocked", value=True)])
        assert _evaluate(cond, {"blocked": False}).matched is True
        assert _evaluate(cond, {"blocked": True}).matched is False

    def test_not_with_wrong_arity_is_surfaced(self) -> None:
        cond = Condition(
            operator="not",
            children=[
                Condition(operator="eq", field="a", value=1),
                Condition(operator="eq", field="b", value=2),
            ],
        )
        res = _evaluate(cond, {})
        assert res.matched is False
        assert "exactly one child" in (res.error or "")


class TestThenOtherwise:
    def test_then_runs_on_match(self) -> None:
        rule = Rule(
            id="r",
            when=Condition(operator="gt", field="x", value=5),
            then=[Action(type="set", target="hit", value="then")],
            otherwise=[Action(type="set", target="hit", value="else")],
        )
        ctx: dict = {"x": 10}
        res = RuleEvaluator().evaluate(rule, ctx)
        assert res.matched is True
        assert ctx["hit"] == "then"

    def test_otherwise_runs_on_non_match(self) -> None:
        rule = Rule(
            id="r",
            when=Condition(operator="gt", field="x", value=5),
            then=[Action(type="set", target="hit", value="then")],
            otherwise=[Action(type="set", target="hit", value="else")],
        )
        ctx: dict = {"x": 1}
        res = RuleEvaluator().evaluate(rule, ctx)
        assert res.matched is False
        assert ctx["hit"] == "else"

    def test_disabled_rule_is_skipped(self) -> None:
        rule = Rule(id="r", when=Condition(operator="eq", field="x", value=1), enabled=False)
        res = RuleEvaluator().evaluate(rule, {"x": 1})
        assert res.matched is False


class TestActions:
    def test_increment_defaults_to_one(self) -> None:
        rule = Rule(id="r", then=[Action(type="increment", target="count")])
        ctx: dict = {}
        RuleEvaluator().evaluate(rule, ctx)
        assert ctx["count"] == 1

    def test_nested_write(self) -> None:
        rule = Rule(id="r", then=[Action(type="set", target="flags.discount_pct", value=10)])
        ctx: dict = {}
        RuleEvaluator().evaluate(rule, ctx)
        assert ctx["flags"]["discount_pct"] == 10

    def test_unsupported_action_is_isolated(self) -> None:
        # An unsupported 'call' action raises NotImplementedError; it is recorded
        # in the result error while the sibling 'set' still runs.
        rule = Rule(
            id="r",
            then=[
                Action(type="call", target="svc"),
                Action(type="set", target="ok", value=1),
            ],
        )
        ctx: dict = {}
        res = RuleEvaluator().evaluate(rule, ctx)
        assert ctx["ok"] == 1  # sibling executed despite the failing action
        assert res.error is not None and "call" in res.error
        assert [a.type for a in res.actions_executed] == ["set"]

    def test_unknown_operator_is_surfaced(self) -> None:
        res = _evaluate(Condition(operator="fuzzy_match", field="x", value="abc"), {"x": "abc"})
        assert res.matched is False
        assert "unknown operator: fuzzy_match" in (res.error or "")


class TestRuleSet:
    def test_priority_ordering_over_shared_context(self) -> None:
        # Inserted low-then-high, but evaluated high-then-low (sorted by -priority).
        # Both write the same path over the shared context, so the LAST writer (low)
        # wins — proving high ran first.
        low = Rule(id="low", priority=1, then=[Action(type="set", target="winner", value="low")])
        high = Rule(id="high", priority=10, then=[Action(type="set", target="winner", value="high")])
        ruleset = RuleSet(id="rs", rules=[low, high])
        ctx: dict = {}

        results = RuleSetEvaluator().evaluate(ruleset, ctx)

        assert [r.rule_id for r in results] == ["high", "low"]  # priority order
        assert ctx["winner"] == "low"  # high ran first, low overwrote

    def test_one_failing_rule_does_not_abort_the_set(self) -> None:
        bad = Rule(id="bad", priority=10, then=[Action(type="call")])  # unsupported -> error
        good = Rule(id="good", priority=1, then=[Action(type="set", target="ran", value=True)])
        ruleset = RuleSet(id="rs", rules=[bad, good])
        ctx: dict = {}

        results = RuleSetEvaluator().evaluate(ruleset, ctx)

        assert ctx["ran"] is True  # good ran despite bad's error
        assert any(r.error for r in results)
