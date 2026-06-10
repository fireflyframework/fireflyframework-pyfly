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
"""Tests for the fluent builder DSL (SP-13 Part A, Item 2).

Each builder-constructed rule is evaluated through RuleSetEvaluator and its
result is compared to an equivalent rule built directly from dataclasses to
confirm behavioural identity.
"""

from __future__ import annotations

from pyfly.rule_engine import (
    Action,
    Condition,
    EvaluationResult,
    Rule,
    RuleSet,
    RuleSetEvaluator,
)
from pyfly.rule_engine.builder import (
    all_of,
    any_of,
    field,
    increment_action,
    log_action,
    not_,
    rule,
    ruleset,
    set_action,
)


def _run(r: Rule, ctx: dict) -> EvaluationResult:  # type: ignore[type-arg]
    rs = RuleSet(id="test-rs", rules=[r])
    results = RuleSetEvaluator().evaluate(rs, ctx)
    return results[0]


# ---------------------------------------------------------------------------
# Leaf operator helpers
# ---------------------------------------------------------------------------


class TestFieldBuilderLeafOps:
    """Each helper method on _FieldBuilder produces the right Condition."""

    def test_eq(self) -> None:
        c = field("x").eq(5)
        assert c.operator == "eq" and c.field == "x" and c.value == 5

    def test_ne(self) -> None:
        c = field("x").ne(5)
        assert c.operator == "ne"

    def test_gt(self) -> None:
        c = field("x").gt(3)
        assert c.operator == "gt" and c.value == 3

    def test_ge(self) -> None:
        assert field("x").ge(3).operator == "ge"

    def test_lt(self) -> None:
        assert field("x").lt(3).operator == "lt"

    def test_le(self) -> None:
        assert field("x").le(3).operator == "le"

    def test_in_(self) -> None:
        c = field("x").in_(["a", "b"])
        assert c.operator == "in" and c.value == ["a", "b"]

    def test_not_in(self) -> None:
        c = field("x").not_in(["a"])
        assert c.operator == "not_in"

    def test_regex(self) -> None:
        c = field("x").regex("^A")
        assert c.operator == "regex" and c.value == "^A"

    def test_between(self) -> None:
        c = field("x").between(1, 10)
        assert c.operator == "between" and c.value == [1, 10]

    def test_contains(self) -> None:
        c = field("x").contains("hello")
        assert c.operator == "contains" and c.value == "hello"

    def test_starts_with(self) -> None:
        c = field("x").starts_with("pre-")
        assert c.operator == "starts_with"

    def test_ends_with(self) -> None:
        c = field("x").ends_with(".pdf")
        assert c.operator == "ends_with"

    def test_exists(self) -> None:
        c = field("x").exists()
        assert c.operator == "exists" and c.value is None

    def test_is_null(self) -> None:
        assert field("x").is_null().operator == "is_null"

    def test_is_empty(self) -> None:
        assert field("x").is_empty().operator == "is_empty"


# ---------------------------------------------------------------------------
# Compound helpers
# ---------------------------------------------------------------------------


class TestCompoundHelpers:
    def test_all_of_produces_and(self) -> None:
        c = all_of(field("a").eq(1), field("b").eq(2))
        assert c.operator == "and" and len(c.children) == 2

    def test_any_of_produces_or(self) -> None:
        c = any_of(field("a").eq(1), field("b").eq(2))
        assert c.operator == "or"

    def test_not_produces_not(self) -> None:
        c = not_(field("x").eq(True))
        assert c.operator == "not" and len(c.children) == 1


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------


class TestActionHelpers:
    def test_set_action(self) -> None:
        a = set_action("flags.ok", True)
        assert a.type == "set" and a.target == "flags.ok" and a.value is True

    def test_increment_action_default(self) -> None:
        a = increment_action("count")
        assert a.type == "increment" and a.target == "count" and a.value == 1

    def test_increment_action_custom(self) -> None:
        a = increment_action("count", by=5)
        assert a.value == 5

    def test_log_action(self) -> None:
        a = log_action("hello")
        assert a.type == "log" and a.value == "hello"


# ---------------------------------------------------------------------------
# RuleBuilder — basic build + evaluation
# ---------------------------------------------------------------------------


class TestRuleBuilder:
    def test_build_returns_rule_with_correct_fields(self) -> None:
        r = (
            rule("my-rule")
            .describe("test rule")
            .priority(5)
            .enabled(True)
            .when(field("x").gt(0))
            .then(set_action("result", "positive"))
            .otherwise(set_action("result", "non-positive"))
            .build()
        )
        assert r.id == "my-rule"
        assert r.description == "test rule"
        assert r.priority == 5
        assert r.enabled is True
        assert r.when is not None
        assert len(r.then) == 1
        assert len(r.otherwise) == 1

    def test_then_actions_run_on_match(self) -> None:
        r = (
            rule("r")
            .when(field("score").ge(100))
            .then(set_action("tier", "gold"))
            .otherwise(set_action("tier", "silver"))
            .build()
        )
        ctx: dict = {"score": 150}
        res = _run(r, ctx)
        assert res.matched is True
        assert ctx["tier"] == "gold"

    def test_otherwise_runs_on_no_match(self) -> None:
        r = (
            rule("r")
            .when(field("score").ge(100))
            .then(set_action("tier", "gold"))
            .otherwise(set_action("tier", "silver"))
            .build()
        )
        ctx: dict = {"score": 50}
        res = _run(r, ctx)
        assert res.matched is False
        assert ctx["tier"] == "silver"

    def test_disabled_rule_is_skipped(self) -> None:
        r = rule("r").enabled(False).when(field("x").eq(1)).then(set_action("ran", True)).build()
        ctx: dict = {"x": 1}
        res = _run(r, ctx)
        assert res.matched is False
        assert "ran" not in ctx


# ---------------------------------------------------------------------------
# Complex nested all_of / any_of / not_
# ---------------------------------------------------------------------------


class TestNestedConditions:
    """Builder-constructed nested rule must behave identically to the equivalent
    dataclass rule."""

    def _dataclass_rule(self) -> Rule:
        """Equivalent rule built from raw dataclasses (the reference)."""
        return Rule(
            id="dc-rule",
            when=Condition(
                operator="and",
                children=[
                    Condition(operator="ge", field="order.total", value=200),
                    Condition(
                        operator="or",
                        children=[
                            Condition(operator="eq", field="customer.tier", value="gold"),
                            Condition(
                                operator="not",
                                children=[Condition(operator="eq", field="customer.blocked", value=True)],
                            ),
                        ],
                    ),
                ],
            ),
            then=[Action(type="set", target="order.discount_pct", value=15)],
            otherwise=[Action(type="set", target="order.discount_pct", value=0)],
        )

    def _builder_rule(self) -> Rule:
        return (
            rule("builder-rule")
            .when(
                all_of(
                    field("order.total").ge(200),
                    any_of(
                        field("customer.tier").eq("gold"),
                        not_(field("customer.blocked").eq(True)),
                    ),
                )
            )
            .then(set_action("order.discount_pct", 15))
            .otherwise(set_action("order.discount_pct", 0))
            .build()
        )

    def _run_both(self, ctx: dict) -> tuple[bool, bool]:  # type: ignore[type-arg]
        ev = RuleSetEvaluator()
        dc_res = ev.evaluate(RuleSet(id="dc", rules=[self._dataclass_rule()]), ctx)
        bu_res = ev.evaluate(RuleSet(id="bu", rules=[self._builder_rule()]), dict(ctx))
        return dc_res[0].matched, bu_res[0].matched

    def test_both_match_high_spend_gold(self) -> None:
        ctx = {"order": {"total": 300}, "customer": {"tier": "gold", "blocked": False}}
        dc_m, bu_m = self._run_both(ctx)
        assert dc_m is True and bu_m is True

    def test_both_match_high_spend_not_blocked(self) -> None:
        ctx = {"order": {"total": 250}, "customer": {"tier": "silver", "blocked": False}}
        dc_m, bu_m = self._run_both(ctx)
        assert dc_m is True and bu_m is True

    def test_both_no_match_low_spend(self) -> None:
        ctx = {"order": {"total": 50}, "customer": {"tier": "gold", "blocked": False}}
        dc_m, bu_m = self._run_both(ctx)
        assert dc_m is False and bu_m is False

    def test_both_no_match_high_spend_blocked_non_gold(self) -> None:
        ctx = {"order": {"total": 300}, "customer": {"tier": "silver", "blocked": True}}
        dc_m, bu_m = self._run_both(ctx)
        assert dc_m is False and bu_m is False

    def test_discount_written_identically(self) -> None:
        """Actions mutate context the same way for both rules."""
        ctx_dc: dict = {"order": {"total": 300}, "customer": {"tier": "gold", "blocked": False}}
        ctx_bu: dict = {"order": {"total": 300}, "customer": {"tier": "gold", "blocked": False}}
        RuleSetEvaluator().evaluate(RuleSet(id="dc", rules=[self._dataclass_rule()]), ctx_dc)
        RuleSetEvaluator().evaluate(RuleSet(id="bu", rules=[self._builder_rule()]), ctx_bu)
        assert ctx_dc["order"]["discount_pct"] == ctx_bu["order"]["discount_pct"] == 15


# ---------------------------------------------------------------------------
# RuleSetBuilder
# ---------------------------------------------------------------------------


class TestRuleSetBuilder:
    def test_build_contains_rules_in_order(self) -> None:
        r1 = rule("r1").priority(1).build()
        r2 = rule("r2").priority(10).build()
        rs = ruleset("my-rs", name="My Rules", version=2).add(r1, r2).build()
        assert rs.id == "my-rs"
        assert rs.name == "My Rules"
        assert rs.version == 2
        assert [r.id for r in rs.rules] == ["r1", "r2"]

    def test_sorted_rules_from_builder(self) -> None:
        r1 = rule("low").priority(1).build()
        r2 = rule("high").priority(10).build()
        rs = ruleset("rs").add(r1, r2).build()
        assert [r.id for r in rs.sorted_rules()] == ["high", "low"]
