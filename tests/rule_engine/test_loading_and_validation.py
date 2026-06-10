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
"""Tests for JSON loading and RuleSet validation (SP-13 Part A, Item 3)."""

from __future__ import annotations

import json

import pytest

from pyfly.rule_engine import (
    Action,
    Condition,
    Rule,
    RuleSet,
    RuleSetLoader,
)
from pyfly.rule_engine.validation import (
    RuleSetValidator,
    RuleValidationError,
    validate_ruleset,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_RULESET_YAML = """\
id: order-rules
name: Order processing
version: 2
rules:
  - id: high-value
    priority: 10
    when:
      op: ge
      field: order.amount
      value: 1000
    then:
      - type: set
        target: flags.high_value
        value: true
  - id: cheap
    priority: 5
    when:
      op: lt
      field: order.amount
      value: 100
    then:
      - type: set
        target: flags.cheap
        value: true
"""

_RULESET_DICT = {
    "id": "order-rules",
    "name": "Order processing",
    "version": 2,
    "rules": [
        {
            "id": "high-value",
            "priority": 10,
            "when": {"op": "ge", "field": "order.amount", "value": 1000},
            "then": [{"type": "set", "target": "flags.high_value", "value": True}],
        },
        {
            "id": "cheap",
            "priority": 5,
            "when": {"op": "lt", "field": "order.amount", "value": 100},
            "then": [{"type": "set", "target": "flags.cheap", "value": True}],
        },
    ],
}


# ---------------------------------------------------------------------------
# from_json
# ---------------------------------------------------------------------------


class TestFromJson:
    def test_parses_json_string(self) -> None:
        text = json.dumps(_RULESET_DICT)
        rs = RuleSetLoader.from_json(text)
        assert rs.id == "order-rules"
        assert rs.name == "Order processing"
        assert rs.version == 2
        assert len(rs.rules) == 2

    def test_round_trips_equal_to_from_yaml(self) -> None:
        """JSON and YAML representations of the same ruleset produce equal objects."""
        from_yaml = RuleSetLoader.from_yaml(_RULESET_YAML)
        from_json = RuleSetLoader.from_json(json.dumps(_RULESET_DICT))

        assert from_yaml.id == from_json.id
        assert from_yaml.name == from_json.name
        assert from_yaml.version == from_json.version
        assert len(from_yaml.rules) == len(from_json.rules)
        for yr, jr in zip(from_yaml.rules, from_json.rules, strict=True):
            assert yr.id == jr.id
            assert yr.priority == jr.priority
            assert yr.enabled == jr.enabled

    def test_rule_conditions_equal(self) -> None:
        from_yaml = RuleSetLoader.from_yaml(_RULESET_YAML)
        from_json = RuleSetLoader.from_json(json.dumps(_RULESET_DICT))
        for yr, jr in zip(from_yaml.rules, from_json.rules, strict=True):
            assert yr.when is not None and jr.when is not None
            assert yr.when.operator == jr.when.operator
            assert yr.when.field == jr.when.field
            assert yr.when.value == jr.when.value

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError):
            RuleSetLoader.from_json("not valid json {{{")


# ---------------------------------------------------------------------------
# validate_ruleset / RuleSetValidator — valid case
# ---------------------------------------------------------------------------


class TestValidatorValidRuleset:
    def test_empty_issues_for_valid_ruleset(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[
                Rule(
                    id="r1",
                    when=Condition(operator="gt", field="x", value=5),
                    then=[Action(type="set", target="y", value=1)],
                ),
                Rule(
                    id="r2",
                    when=Condition(operator="between", field="x", value=[1, 10]),
                ),
            ],
        )
        assert validate_ruleset(rs) == []

    def test_assert_valid_does_not_raise_for_valid(self) -> None:
        rs = RuleSet(id="rs", rules=[Rule(id="r1")])
        RuleSetValidator.assert_valid(rs)  # must not raise


# ---------------------------------------------------------------------------
# validate_ruleset — invalid cases
# ---------------------------------------------------------------------------


class TestValidatorInvalidCases:
    def test_duplicate_rule_ids(self) -> None:
        rs = RuleSet(id="rs", rules=[Rule(id="dup"), Rule(id="dup")])
        issues = validate_ruleset(rs)
        assert any("duplicate" in i and "dup" in i for i in issues)

    def test_unknown_operator(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[
                Rule(
                    id="r1",
                    when=Condition(operator="fuzzy_match", field="x", value="abc"),
                )
            ],
        )
        issues = validate_ruleset(rs)
        assert any("fuzzy_match" in i for i in issues)

    def test_missing_target_on_set(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[Rule(id="r1", then=[Action(type="set", target=None, value=1)])],
        )
        issues = validate_ruleset(rs)
        assert any("target" in i and "set" in i for i in issues)

    def test_missing_target_on_increment(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[Rule(id="r1", then=[Action(type="increment", target=None)])],
        )
        issues = validate_ruleset(rs)
        assert any("target" in i and "increment" in i for i in issues)

    def test_bad_between_value_not_two_elements(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[
                Rule(
                    id="r1",
                    when=Condition(operator="between", field="x", value=[1, 2, 3]),
                )
            ],
        )
        issues = validate_ruleset(rs)
        assert any("between" in i for i in issues)

    def test_bad_between_value_scalar(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[
                Rule(
                    id="r1",
                    when=Condition(operator="between", field="x", value=5),
                )
            ],
        )
        issues = validate_ruleset(rs)
        assert any("between" in i for i in issues)

    def test_unknown_action_type(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[Rule(id="r1", then=[Action(type="teleport", target="x")])],
        )
        issues = validate_ruleset(rs)
        assert any("teleport" in i for i in issues)

    def test_compound_and_with_no_children(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[Rule(id="r1", when=Condition(operator="and", children=[]))],
        )
        issues = validate_ruleset(rs)
        assert any("and" in i for i in issues)

    def test_not_with_two_children(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[
                Rule(
                    id="r1",
                    when=Condition(
                        operator="not",
                        children=[
                            Condition(operator="eq", field="a", value=1),
                            Condition(operator="eq", field="b", value=2),
                        ],
                    ),
                )
            ],
        )
        issues = validate_ruleset(rs)
        assert any("not" in i for i in issues)

    def test_multiple_issues_all_reported(self) -> None:
        rs = RuleSet(
            id="rs",
            rules=[
                Rule(id="dup"),
                Rule(
                    id="dup",
                    when=Condition(operator="bad_op", field="x"),
                    then=[Action(type="set")],
                ),
            ],
        )
        issues = validate_ruleset(rs)
        assert len(issues) >= 3  # duplicate + unknown op + missing target

    def test_assert_valid_raises_rule_validation_error(self) -> None:
        rs = RuleSet(id="rs", rules=[Rule(id="dup"), Rule(id="dup")])
        with pytest.raises(RuleValidationError) as exc_info:
            RuleSetValidator.assert_valid(rs)
        err = exc_info.value
        assert err.ruleset_id == "rs"
        assert len(err.issues) >= 1
        assert "dup" in str(err)
