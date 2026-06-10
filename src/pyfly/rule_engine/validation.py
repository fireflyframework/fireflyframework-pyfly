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
"""Static validation for :class:`~pyfly.rule_engine.dsl.RuleSet` objects.

Use :func:`validate_ruleset` to get a list of human-readable issues, or
:class:`RuleSetValidator` for an object-oriented interface that also provides
:meth:`~RuleSetValidator.assert_valid` (raises :class:`RuleValidationError`).

Example::

    from pyfly.rule_engine.validation import RuleSetValidator, RuleValidationError

    issues = RuleSetValidator.check(my_ruleset)
    if issues:
        raise RuleValidationError(my_ruleset.id, issues)
"""

from __future__ import annotations

from collections.abc import Sequence

from pyfly.rule_engine.dsl import Action, Condition, RuleSet

# ---------------------------------------------------------------------------
# Known operator/action sets
# ---------------------------------------------------------------------------

_LEAF_OPERATORS: frozenset[str] = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "ge",
        "lt",
        "le",
        "in",
        "not_in",
        "regex",
        "between",
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "exists",
        "is_null",
        "is_empty",
    }
)

_COMPOUND_OPERATORS: frozenset[str] = frozenset({"and", "or", "not"})

_KNOWN_ACTION_TYPES: frozenset[str] = frozenset({"set", "increment", "log", "call", "calculate"})

_TARGET_REQUIRED: frozenset[str] = frozenset({"set", "increment"})


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class RuleValidationError(ValueError):
    """Raised by :meth:`RuleSetValidator.assert_valid` when issues are found."""

    def __init__(self, ruleset_id: str, issues: Sequence[str]) -> None:
        self.ruleset_id = ruleset_id
        self.issues = list(issues)
        joined = "; ".join(issues)
        super().__init__(f"RuleSet '{ruleset_id}' has {len(issues)} validation issue(s): {joined}")


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_ruleset(ruleset: RuleSet) -> list[str]:
    """Return a list of human-readable validation issues for *ruleset*.

    An empty list means the ruleset is valid.  Issues include:

    * Duplicate rule IDs.
    * An unknown leaf operator (not in the supported set).
    * A compound op (``and`` / ``or``) with no children, or ``not`` with
      a child count other than 1.
    * A ``set`` or ``increment`` action missing *target*.
    * A ``between`` condition whose *value* is not a 2-element sequence.
    * An unknown action type.
    """
    return RuleSetValidator.check(ruleset)


class RuleSetValidator:
    """Object-oriented wrapper around :func:`validate_ruleset`."""

    @staticmethod
    def check(ruleset: RuleSet) -> list[str]:
        """Return validation issues; empty list = valid."""
        issues: list[str] = []
        seen_ids: set[str] = set()

        for rule in ruleset.rules:
            if rule.id in seen_ids:
                issues.append(f"duplicate rule id '{rule.id}'")
            else:
                seen_ids.add(rule.id)

            if rule.when is not None:
                _check_condition(rule.when, rule.id, issues)

            for action in rule.then:
                _check_action(action, rule.id, "then", issues)
            for action in rule.otherwise:
                _check_action(action, rule.id, "otherwise", issues)

        return issues

    @staticmethod
    def assert_valid(ruleset: RuleSet) -> None:
        """Raise :class:`RuleValidationError` if there are any issues."""
        issues = RuleSetValidator.check(ruleset)
        if issues:
            raise RuleValidationError(ruleset.id, issues)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_condition(cond: Condition, rule_id: str, issues: list[str]) -> None:
    op = cond.operator
    if op in _COMPOUND_OPERATORS:
        if op == "not":
            if len(cond.children) != 1:
                issues.append(f"rule '{rule_id}': 'not' requires exactly 1 child, got {len(cond.children)}")
        else:
            if len(cond.children) == 0:
                issues.append(f"rule '{rule_id}': compound op '{op}' has no children")
        for child in cond.children:
            _check_condition(child, rule_id, issues)
    elif op not in _LEAF_OPERATORS:
        issues.append(f"rule '{rule_id}': unknown operator '{op}'")
    else:
        if op == "between":
            v = cond.value
            try:
                if not hasattr(v, "__len__") or len(v) != 2:
                    issues.append(f"rule '{rule_id}': 'between' value must be a 2-element sequence, got {v!r}")
            except TypeError:
                issues.append(f"rule '{rule_id}': 'between' value must be a 2-element sequence, got {v!r}")


def _check_action(action: Action, rule_id: str, branch: str, issues: list[str]) -> None:
    if action.type not in _KNOWN_ACTION_TYPES:
        issues.append(f"rule '{rule_id}' ({branch}): unknown action type '{action.type}'")
    if action.type in _TARGET_REQUIRED and not action.target:
        issues.append(f"rule '{rule_id}' ({branch}): '{action.type}' action missing 'target'")
