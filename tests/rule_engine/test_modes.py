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
"""Tests for EvaluationMode (SP-13 Part B Item 2)."""

from __future__ import annotations

from pyfly.rule_engine.dsl import Action, Condition, Rule, RuleSet
from pyfly.rule_engine.evaluator import EvaluationMode, RuleSetEvaluator


def _make_ruleset() -> RuleSet:
    """Two rules: high-priority (p=10) matches, low-priority (p=1) also matches."""
    high = Rule(
        id="high",
        priority=10,
        when=Condition(operator="eq", field="tier", value="gold"),
        then=[Action(type="set", target="high_ran", value=True)],
    )
    low = Rule(
        id="low",
        priority=1,
        when=Condition(operator="eq", field="tier", value="gold"),
        then=[Action(type="set", target="low_ran", value=True)],
    )
    return RuleSet(id="rs", rules=[high, low])


def _make_ruleset_no_match_high() -> RuleSet:
    """High rule does NOT match; low rule matches."""
    high = Rule(
        id="high",
        priority=10,
        when=Condition(operator="eq", field="tier", value="platinum"),
        then=[Action(type="set", target="high_ran", value=True)],
    )
    low = Rule(
        id="low",
        priority=1,
        when=Condition(operator="eq", field="tier", value="gold"),
        then=[Action(type="set", target="low_ran", value=True)],
    )
    return RuleSet(id="rs", rules=[high, low])


class TestAllMode:
    def test_all_evaluates_every_rule(self) -> None:
        evaluator = RuleSetEvaluator(mode=EvaluationMode.ALL)
        ctx: dict = {"tier": "gold"}
        results = evaluator.evaluate(_make_ruleset(), ctx)

        assert len(results) == 2
        assert [r.rule_id for r in results] == ["high", "low"]

    def test_all_fires_actions_for_all_matching_rules(self) -> None:
        evaluator = RuleSetEvaluator(mode=EvaluationMode.ALL)
        ctx: dict = {"tier": "gold"}
        evaluator.evaluate(_make_ruleset(), ctx)

        assert ctx.get("high_ran") is True
        assert ctx.get("low_ran") is True

    def test_all_is_default_mode(self) -> None:
        """RuleSetEvaluator() defaults to ALL."""
        evaluator = RuleSetEvaluator()
        ctx: dict = {"tier": "gold"}
        results = evaluator.evaluate(_make_ruleset(), ctx)

        assert len(results) == 2
        assert ctx.get("low_ran") is True

    def test_all_context_mutations_are_visible_to_later_rules(self) -> None:
        """Earlier rules (higher priority) mutate ctx before later rules see it."""
        set_rule = Rule(
            id="setter",
            priority=10,
            then=[Action(type="set", target="x", value=1)],
        )
        read_rule = Rule(
            id="reader",
            priority=1,
            when=Condition(operator="eq", field="x", value=1),
            then=[Action(type="set", target="read_ok", value=True)],
        )
        ruleset = RuleSet(id="rs", rules=[set_rule, read_rule])
        ctx: dict = {}
        RuleSetEvaluator(mode=EvaluationMode.ALL).evaluate(ruleset, ctx)
        assert ctx.get("read_ok") is True


class TestFirstMatchMode:
    def test_first_match_stops_after_first_matching_rule(self) -> None:
        evaluator = RuleSetEvaluator(mode=EvaluationMode.FIRST_MATCH)
        ctx: dict = {"tier": "gold"}
        results = evaluator.evaluate(_make_ruleset(), ctx)

        # Only the high-priority rule should appear in results
        assert len(results) == 1
        assert results[0].rule_id == "high"
        assert results[0].matched is True

    def test_first_match_lower_priority_actions_do_not_fire(self) -> None:
        evaluator = RuleSetEvaluator(mode=EvaluationMode.FIRST_MATCH)
        ctx: dict = {"tier": "gold"}
        evaluator.evaluate(_make_ruleset(), ctx)

        assert ctx.get("high_ran") is True
        assert "low_ran" not in ctx, "low-priority rule must NOT have executed"

    def test_first_match_continues_past_non_matching_rules(self) -> None:
        """In FIRST_MATCH, a rule that does NOT match is included in results
        but does not stop iteration — only a *match* stops it.
        """
        evaluator = RuleSetEvaluator(mode=EvaluationMode.FIRST_MATCH)
        ctx: dict = {"tier": "gold"}
        results = evaluator.evaluate(_make_ruleset_no_match_high(), ctx)

        # high did NOT match → evaluation continues; low DOES match → stops
        assert len(results) == 2
        assert results[0].rule_id == "high"
        assert results[0].matched is False
        assert results[1].rule_id == "low"
        assert results[1].matched is True
        assert ctx.get("low_ran") is True

    def test_first_match_returns_all_when_no_rule_matches(self) -> None:
        """When nothing matches in FIRST_MATCH the full list is returned."""
        evaluator = RuleSetEvaluator(mode=EvaluationMode.FIRST_MATCH)
        ctx: dict = {"tier": "bronze"}
        results = evaluator.evaluate(_make_ruleset(), ctx)

        # Neither high nor low matches bronze — all rules evaluated, nothing fired
        assert len(results) == 2
        assert not any(r.matched for r in results)
        assert "high_ran" not in ctx
        assert "low_ran" not in ctx

    def test_first_match_single_rule_match(self) -> None:
        """A one-rule ruleset that matches stops immediately."""
        ruleset = RuleSet(
            id="rs",
            rules=[Rule(id="only", then=[Action(type="set", target="fired", value=True)])],
        )
        evaluator = RuleSetEvaluator(mode=EvaluationMode.FIRST_MATCH)
        ctx: dict = {}
        results = evaluator.evaluate(ruleset, ctx)

        assert len(results) == 1
        assert ctx.get("fired") is True

    def test_first_match_shared_context_mutations_visible(self) -> None:
        """FIRST_MATCH shares context with ALL semantics for evaluated rules."""
        high = Rule(
            id="high",
            priority=10,
            then=[Action(type="set", target="x", value=1)],
        )
        low = Rule(
            id="low",
            priority=1,
            when=Condition(operator="eq", field="x", value=1),
            then=[Action(type="set", target="low_ran", value=True)],
        )
        ruleset = RuleSet(id="rs", rules=[high, low])
        ctx: dict = {}
        # high matches (no condition), so FIRST_MATCH stops after high
        results = RuleSetEvaluator(mode=EvaluationMode.FIRST_MATCH).evaluate(ruleset, ctx)

        assert len(results) == 1
        assert ctx.get("x") == 1
        assert "low_ran" not in ctx
