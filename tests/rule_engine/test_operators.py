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
"""Tests for all new rich operators added in SP-13 Part A.

Each operator is covered with:
  - a "true" case,
  - a "false" case,
  - a None/missing-field case (must return False without crashing).
"""

from __future__ import annotations

import pytest

from pyfly.rule_engine import Condition, Rule, RuleEvaluator


def _eval(cond: Condition, ctx: dict) -> bool:  # type: ignore[type-arg]
    return RuleEvaluator().evaluate(Rule(id="t", when=cond), ctx).matched


# ---------------------------------------------------------------------------
# between
# ---------------------------------------------------------------------------


class TestBetween:
    def test_within_range(self) -> None:
        cond = Condition(operator="between", field="x", value=[1, 10])
        assert _eval(cond, {"x": 5}) is True

    def test_at_lower_boundary(self) -> None:
        cond = Condition(operator="between", field="x", value=[1, 10])
        assert _eval(cond, {"x": 1}) is True

    def test_at_upper_boundary(self) -> None:
        cond = Condition(operator="between", field="x", value=[1, 10])
        assert _eval(cond, {"x": 10}) is True

    def test_below_range(self) -> None:
        cond = Condition(operator="between", field="x", value=[5, 10])
        assert _eval(cond, {"x": 4}) is False

    def test_above_range(self) -> None:
        cond = Condition(operator="between", field="x", value=[5, 10])
        assert _eval(cond, {"x": 11}) is False

    def test_none_field_is_false(self) -> None:
        cond = Condition(operator="between", field="missing", value=[1, 10])
        assert _eval(cond, {}) is False


# ---------------------------------------------------------------------------
# contains
# ---------------------------------------------------------------------------


class TestContains:
    def test_substring_true(self) -> None:
        cond = Condition(operator="contains", field="s", value="hello")
        assert _eval(cond, {"s": "say hello world"}) is True

    def test_substring_false(self) -> None:
        cond = Condition(operator="contains", field="s", value="hello")
        assert _eval(cond, {"s": "no greeting here"}) is False

    def test_list_member_true(self) -> None:
        cond = Condition(operator="contains", field="tags", value="vip")
        assert _eval(cond, {"tags": ["standard", "vip", "new"]}) is True

    def test_list_member_false(self) -> None:
        cond = Condition(operator="contains", field="tags", value="vip")
        assert _eval(cond, {"tags": ["standard", "new"]}) is False

    def test_none_field_is_false(self) -> None:
        cond = Condition(operator="contains", field="missing", value="x")
        assert _eval(cond, {}) is False


# ---------------------------------------------------------------------------
# not_contains
# ---------------------------------------------------------------------------


class TestNotContains:
    def test_substring_absent_true(self) -> None:
        cond = Condition(operator="not_contains", field="s", value="bad")
        assert _eval(cond, {"s": "good text"}) is True

    def test_substring_present_false(self) -> None:
        cond = Condition(operator="not_contains", field="s", value="bad")
        assert _eval(cond, {"s": "this is bad"}) is False

    def test_list_absent_true(self) -> None:
        cond = Condition(operator="not_contains", field="tags", value="blocked")
        assert _eval(cond, {"tags": ["active", "vip"]}) is True

    def test_list_present_false(self) -> None:
        cond = Condition(operator="not_contains", field="tags", value="blocked")
        assert _eval(cond, {"tags": ["active", "blocked"]}) is False

    def test_none_field_is_false(self) -> None:
        cond = Condition(operator="not_contains", field="missing", value="x")
        assert _eval(cond, {}) is False


# ---------------------------------------------------------------------------
# starts_with
# ---------------------------------------------------------------------------


class TestStartsWith:
    def test_true(self) -> None:
        cond = Condition(operator="starts_with", field="code", value="ACME-")
        assert _eval(cond, {"code": "ACME-001"}) is True

    def test_false(self) -> None:
        cond = Condition(operator="starts_with", field="code", value="ACME-")
        assert _eval(cond, {"code": "OTHER-001"}) is False

    def test_none_field_is_false(self) -> None:
        cond = Condition(operator="starts_with", field="missing", value="ACME-")
        assert _eval(cond, {}) is False


# ---------------------------------------------------------------------------
# ends_with
# ---------------------------------------------------------------------------


class TestEndsWith:
    def test_true(self) -> None:
        cond = Condition(operator="ends_with", field="filename", value=".pdf")
        assert _eval(cond, {"filename": "report.pdf"}) is True

    def test_false(self) -> None:
        cond = Condition(operator="ends_with", field="filename", value=".pdf")
        assert _eval(cond, {"filename": "report.docx"}) is False

    def test_none_field_is_false(self) -> None:
        cond = Condition(operator="ends_with", field="missing", value=".pdf")
        assert _eval(cond, {}) is False


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


class TestExists:
    def test_field_present_and_non_none(self) -> None:
        cond = Condition(operator="exists", field="name")
        assert _eval(cond, {"name": "Alice"}) is True

    def test_field_absent(self) -> None:
        cond = Condition(operator="exists", field="name")
        assert _eval(cond, {}) is False

    def test_field_explicitly_none(self) -> None:
        cond = Condition(operator="exists", field="name")
        assert _eval(cond, {"name": None}) is False

    def test_value_is_ignored(self) -> None:
        # exists ignores the value key entirely
        cond = Condition(operator="exists", field="x", value="anything")
        assert _eval(cond, {"x": 0}) is True  # falsy but present

    def test_from_dict_tolerates_missing_value(self) -> None:
        cond = Condition.from_dict({"op": "exists", "field": "x"})
        assert _eval(cond, {"x": 42}) is True


# ---------------------------------------------------------------------------
# is_null
# ---------------------------------------------------------------------------


class TestIsNull:
    def test_none_value(self) -> None:
        cond = Condition(operator="is_null", field="x")
        assert _eval(cond, {"x": None}) is True

    def test_absent_field(self) -> None:
        cond = Condition(operator="is_null", field="x")
        assert _eval(cond, {}) is True

    def test_non_null_value(self) -> None:
        cond = Condition(operator="is_null", field="x")
        assert _eval(cond, {"x": 0}) is False

    def test_from_dict_tolerates_missing_value(self) -> None:
        cond = Condition.from_dict({"op": "is_null", "field": "x"})
        assert _eval(cond, {}) is True


# ---------------------------------------------------------------------------
# is_empty
# ---------------------------------------------------------------------------


class TestIsEmpty:
    @pytest.mark.parametrize(
        "value",
        [None, "", [], {}],
    )
    def test_empty_variants(self, value: object) -> None:
        cond = Condition(operator="is_empty", field="x")
        assert _eval(cond, {"x": value}) is True

    def test_absent_field(self) -> None:
        cond = Condition(operator="is_empty", field="x")
        assert _eval(cond, {}) is True  # absent → None → empty

    @pytest.mark.parametrize(
        "value",
        ["hello", [1], {"a": 1}, 0, False],
    )
    def test_non_empty_variants(self, value: object) -> None:
        cond = Condition(operator="is_empty", field="x")
        assert _eval(cond, {"x": value}) is False

    def test_from_dict_tolerates_missing_value(self) -> None:
        cond = Condition.from_dict({"op": "is_empty", "field": "x"})
        assert _eval(cond, {"x": []}) is True
